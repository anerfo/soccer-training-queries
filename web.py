"""Small Flask UI for editing polls and managing the send schedule."""

from __future__ import annotations

import calendar as calendar_module
import logging
import os
import threading
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

MONTHS_DE = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)
WEEKDAYS_SHORT_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

log = logging.getLogger("wa.web")


def _ctx() -> dict[str, Any]:
    return current_app.config["CTX"]


def _fallback_schedule(ctx: dict[str, Any]) -> dict[str, Any]:
    schedule = ctx["normalize_schedule"]({})
    schedule["group"] = ctx.get("default_group", "")
    return schedule


def _schedule_or_default(
    ctx: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    try:
        return ctx["load_schedule"](ctx["schedule_path"]), None
    except ValueError as exc:
        return _fallback_schedule(ctx), str(exc)


def _read_polls(
    ctx: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], str | None]:
    try:
        text = ctx["polls_path"].read_text(encoding="utf-8")
    except FileNotFoundError:
        return "", [], "polls.txt wurde nicht gefunden"
    except OSError as exc:
        return "", [], f"polls.txt kann nicht gelesen werden: {exc}"

    try:
        polls = ctx["parse_polls"](text)
    except ValueError as exc:
        return text, [], str(exc)
    return text, polls, None


def _month_param(value: str | None) -> tuple[int, int, str]:
    if value:
        try:
            parsed = datetime.strptime(value, "%Y-%m")
            return parsed.year, parsed.month, f"{parsed.year:04d}-{parsed.month:02d}"
        except ValueError:
            pass
    today = date.today()
    return today.year, today.month, f"{today.year:04d}-{today.month:02d}"


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int, str]:
    index = year * 12 + (month - 1) + delta
    new_year, new_month = divmod(index, 12)
    new_month += 1
    return new_year, new_month, f"{new_year:04d}-{new_month:02d}"


def _calendar_weeks(
    year: int, month: int, schedule: dict[str, Any], today: date
) -> list[list[dict[str, Any]]]:
    skip = set(schedule["skip_dates"])
    extra = set(schedule["extra_dates"])
    weeks: list[list[dict[str, Any]]] = []

    for week in calendar_module.Calendar(firstweekday=0).monthdatescalendar(
        year, month
    ):
        days = []
        for day in week:
            iso = day.isoformat()
            weekly = day.weekday() == schedule["weekday"]
            is_extra = iso in extra
            scheduled = weekly or is_extra
            skipped = scheduled and iso in skip
            days.append(
                {
                    "date": day,
                    "in_month": day.month == month,
                    "clickable": scheduled,
                    "scheduled": scheduled and not skipped,
                    "skipped": skipped,
                    "extra": is_extra and not weekly and not skipped,
                    "today": day == today,
                }
            )
        weeks.append(days)
    return weeks


def _render_schedule(
    ctx: dict[str, Any],
    schedule: dict[str, Any],
    year: int,
    month: int,
    error: str | None = None,
    status: int = 200,
) -> tuple[str, int]:
    timezone = ZoneInfo(schedule["timezone"])
    now = datetime.now(timezone)
    weeks = _calendar_weeks(year, month, schedule, now.date())
    next_slots = ctx["next_schedule_slots"](schedule, now, 5)
    state = ctx["load_schedule_state"](ctx["schedule_state_path"])
    _prev_year, _prev_month, prev_value = _shift_month(year, month, -1)
    _next_year, _next_month, next_value = _shift_month(year, month, 1)

    html = render_template(
        "schedule.html",
        active="schedule",
        schedule=schedule,
        weeks=weeks,
        year=year,
        month=month,
        month_label=MONTHS_DE[month - 1],
        month_param=f"{year:04d}-{month:02d}",
        prev_month=prev_value,
        next_month=next_value,
        next_slots=next_slots,
        sched_state=state,
        error=error,
        weekdays=ctx["weekdays"],
        weekdays_short=WEEKDAYS_SHORT_DE,
    )
    return html, status


def create_app(service: Any, ctx: dict[str, Any]) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.urandom(24)
    app.config["CTX"] = ctx
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def index() -> str:
        schedule, schedule_error = _schedule_or_default(_ctx())
        now = datetime.now(ZoneInfo(schedule["timezone"]))
        _text, polls, polls_error = _read_polls(_ctx())
        return render_template(
            "index.html",
            active="dashboard",
            status=service.status(),
            schedule=schedule,
            schedule_error=schedule_error,
            polls_error=polls_error,
            poll_count=len(polls),
            next_slots=_ctx()["next_schedule_slots"](schedule, now, 5),
            sched_state=_ctx()["load_schedule_state"](
                _ctx()["schedule_state_path"]
            ),
            weekdays=_ctx()["weekdays"],
        )

    @app.get("/qr.png")
    def qr_code() -> Any:
        if service.connected.is_set():
            abort(404)
        qr_path = _ctx()["qr_path"]
        if not qr_path.is_file():
            abort(404)
        response = send_file(qr_path, mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        return response

    @app.get("/polls")
    def polls_page() -> str:
        text, polls, error = _read_polls(_ctx())
        return render_template(
            "polls.html",
            active="polls",
            text=text,
            polls=polls,
            error=error,
        )

    @app.post("/polls")
    def save_polls() -> Any:
        ctx = _ctx()
        text = request.form.get("polls", "")
        if not text.strip():
            return (
                render_template(
                    "polls.html",
                    active="polls",
                    text=text,
                    polls=[],
                    error="Die Liste ist leer.",
                ),
                400,
            )

        try:
            polls = ctx["parse_polls"](text)
        except ValueError as exc:
            return (
                render_template(
                    "polls.html",
                    active="polls",
                    text=text,
                    polls=[],
                    error=str(exc),
                ),
                400,
            )

        if not text.endswith("\n"):
            text += "\n"
        try:
            # Write in place: polls.txt is commonly a bind-mounted Docker file.
            ctx["polls_path"].write_text(text, encoding="utf-8")
        except OSError as exc:
            flash(f"Speichern fehlgeschlagen: {exc}", "error")
            return (
                render_template(
                    "polls.html",
                    active="polls",
                    text=text,
                    polls=polls,
                    error=str(exc),
                ),
                500,
            )

        flash(f"{len(polls)} Umfragen gespeichert.", "success")
        return redirect(url_for("polls_page"))

    @app.get("/schedule")
    def schedule_page() -> Any:
        ctx = _ctx()
        schedule, error = _schedule_or_default(ctx)
        year, month, _value = _month_param(request.args.get("month"))
        return _render_schedule(ctx, schedule, year, month, error)

    @app.post("/schedule")
    def save_schedule_page() -> Any:
        ctx = _ctx()
        existing, _error = _schedule_or_default(ctx)
        payload = {
            "enabled": request.form.get("enabled") == "on",
            "weekday": request.form.get("weekday", "2"),
            "time": request.form.get("time", "18:00"),
            "timezone": request.form.get("timezone", "Europe/Berlin"),
            "group": request.form.get("group", ""),
            "skip_dates": existing["skip_dates"],
            "extra_dates": request.form.get("extra_dates", ""),
        }
        try:
            schedule = ctx["normalize_schedule"](payload)
            ctx["save_schedule"](ctx["schedule_path"], schedule)
        except (ValueError, OSError) as exc:
            flash(f"Zeitplan nicht gespeichert: {exc}", "error")
            return redirect(
                url_for(
                    "schedule_page", month=request.form.get("month", "")
                )
            )

        flash("Zeitplan gespeichert.", "success")
        return redirect(
            url_for("schedule_page", month=request.form.get("month", ""))
        )

    @app.post("/schedule/toggle")
    def toggle_date() -> Any:
        ctx = _ctx()
        value = request.form.get("date", "")
        month = request.form.get("month", "")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            flash("Ungültiges Datum.", "error")
            return redirect(url_for("schedule_page", month=month))

        schedule, _error = _schedule_or_default(ctx)
        skip = set(schedule["skip_dates"])
        if parsed.isoformat() in skip:
            skip.remove(parsed.isoformat())
            message = f"{parsed.strftime('%d.%m.%Y')} ist wieder aktiv."
        else:
            skip.add(parsed.isoformat())
            message = f"{parsed.strftime('%d.%m.%Y')} wird übersprungen."

        schedule["skip_dates"] = sorted(skip)
        try:
            ctx["save_schedule"](ctx["schedule_path"], schedule)
        except (ValueError, OSError) as exc:
            flash(f"Änderung nicht gespeichert: {exc}", "error")
        else:
            flash(message, "success")
        return redirect(url_for("schedule_page", month=month))

    @app.post("/schedule/clear-skips")
    def clear_skips() -> Any:
        ctx = _ctx()
        month = request.form.get("month", "")
        schedule, _error = _schedule_or_default(ctx)
        schedule["skip_dates"] = []
        try:
            ctx["save_schedule"](ctx["schedule_path"], schedule)
        except (ValueError, OSError) as exc:
            flash(f"Änderung nicht gespeichert: {exc}", "error")
        else:
            flash("Alle Ausnahmen entfernt.", "success")
        return redirect(url_for("schedule_page", month=month))

    @app.post("/send-now")
    def send_now() -> Any:
        ctx = _ctx()
        schedule, _error = _schedule_or_default(ctx)
        group = (
            request.form.get("group")
            or schedule["group"]
            or ctx.get("default_group", "")
        ).strip()
        if not group:
            flash("Keine Gruppe konfiguriert.", "error")
            return redirect(url_for("index"))
        if not service.connected.is_set():
            flash("Nicht mit WhatsApp verbunden.", "error")
            return redirect(url_for("index"))

        try:
            result = ctx["send_next_poll"](
                service,
                ctx["polls_path"],
                ctx["poll_state_path"],
                group,
            )
        except Exception as exc:
            log.exception("manual send failed")
            flash(f"Senden fehlgeschlagen: {exc}", "error")
        else:
            flash(
                f"Umfrage {result['index'] + 1}/{result['total']} gesendet: "
                f"{result['poll']['question']}",
                "success",
            )
        return redirect(url_for("index"))

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return {"ok": True, **service.status()}

    return app


def start_web(app: Flask, host: str, port: int) -> Any:
    from werkzeug.serving import make_server

    server = make_server(host, port, app, threaded=True)
    thread = threading.Thread(
        target=server.serve_forever, name="wa-web", daemon=True
    )
    thread.start()
    server._wa_thread = thread  # type: ignore[attr-defined]
    return server
