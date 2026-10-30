"""Small Flask UI for editing polls and managing the send schedule."""

from __future__ import annotations

import calendar as calendar_module
import hashlib
import logging
import os
import random
import secrets
import threading
import time
import uuid
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import escape

from auth import (
    InvalidToken,
    MissingToken,
    build_auth_from_env,
    new_pkce_pair,
)
from calendar_sync import (
    CalendarError,
    load_calendar_config,
    new_id as new_calendar_id,
    parse_offset,
    save_calendar_config,
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

SIMULATION_PEOPLE = [
    ("491700000001", "Anna"),
    ("491700000002", "Ben"),
    ("491700000003", "Clara"),
    ("491700000004", "David"),
    ("491700000005", "Emma"),
    ("491700000006", "Felix"),
    ("491700000007", "Greta"),
    ("491700000008", "Hannes"),
    ("491700000009", "Ida"),
    ("491700000010", "Jonas"),
    ("491700000011", "Katrin"),
    ("491700000012", "Lukas"),
    ("491700000013", "Mia"),
    ("491700000014", "Nils"),
    ("491700000015", "Olga"),
    ("491700000016", "Paul"),
    ("491700000017", "Rita"),
    ("491700000018", "Simon"),
    ("491700000019", "Tina"),
    ("491700000020", "Uwe"),
]

SIMULATION_FALLBACK_POLL = {
    "question": "Simulierte Umfrage – bist du dabei?",
    "options": ["Ich bin am Start", "Ich bin raus"],
}


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


def _load_history(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    path = ctx.get("poll_history_path")
    loader = ctx.get("load_poll_history")
    if not path or not loader:
        return []
    try:
        return loader(path).get("polls", [])
    except Exception:
        log.exception("could not load poll history")
        return []


def _load_contact_map(ctx: dict[str, Any]) -> dict[str, str]:
    path = ctx.get("contacts_path")
    loader = ctx.get("load_contacts")
    if not path or not loader:
        return {}
    try:
        return loader(path)
    except Exception:
        log.exception("could not load contacts")
        return {}


def _calendar_config(ctx: dict[str, Any]) -> dict[str, Any]:
    path = ctx.get("calendar_config_path")
    if not path:
        return {"calendars": [], "rules": []}
    try:
        return load_calendar_config(path)
    except CalendarError:
        log.exception("could not load calendar config")
        return {"calendars": [], "rules": []}


def _jid_number(jid: str) -> str:
    if not jid:
        return ""
    return jid.split("@", 1)[0].split(":", 1)[0]


def _vote_identity(vote: dict[str, Any]) -> tuple[str, str]:
    """Return (display JID, phone number), preferring the phone-number JID."""
    sender = vote.get("sender") or ""
    sender_alt = vote.get("sender_alt") or ""
    chosen = sender
    for candidate in (sender_alt, sender):
        if candidate and candidate.endswith("@s.whatsapp.net"):
            chosen = candidate
            break
    if not chosen:
        chosen = sender_alt
    return chosen, _jid_number(chosen)


def _vote_rows(
    poll: dict[str, Any] | None, contacts: dict[str, str]
) -> list[dict[str, Any]]:
    if not poll:
        return []
    rows = []
    for key, vote in (poll.get("votes") or {}).items():
        jid, number = _vote_identity(vote)
        name = (
            contacts.get(number)
            or contacts.get(jid)
            or contacts.get(key)
            or ""
        )
        rows.append(
            {
                "name": name,
                "number": number,
                "jid": jid or key,
                "pushname": vote.get("pushname") or "",
                "selected": vote.get("selected") or [],
                "timestamp": vote.get("timestamp") or 0,
            }
        )
    rows.sort(
        key=lambda row: (
            row["name"] or row["pushname"] or row["number"] or row["jid"]
        ).casefold()
    )
    return rows


def _collect_numbers(polls: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    for poll in polls:
        for vote in (poll.get("votes") or {}).values():
            _jid, number = _vote_identity(vote)
            if number:
                seen.add(number)
    return sorted(seen)


def _whatsapp_names(polls: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for poll in polls:
        for vote in (poll.get("votes") or {}).values():
            _jid, number = _vote_identity(vote)
            pushname = (vote.get("pushname") or "").strip()
            if number and pushname:
                names[number] = pushname
    return names


def _contact_rows(
    contacts: dict[str, str],
    numbers: list[str],
    whatsapp_names: dict[str, str],
) -> list[dict[str, str]]:
    all_numbers = sorted(set(numbers) | set(contacts))
    rows = [
        {
            "number": number,
            "name": contacts.get(number, ""),
            "whatsapp": whatsapp_names.get(number, ""),
        }
        for number in all_numbers
    ]
    rows.sort(key=lambda row: ((row["name"] or "~"), row["number"]))
    return rows


def _display_name(row: dict[str, Any]) -> str:
    return (
        row.get("name")
        or row.get("pushname")
        or row.get("number")
        or row.get("jid")
        or ""
    ).strip()


def _names_export(rows: list[dict[str, Any]]) -> str:
    names = [_display_name(row) for row in rows]
    names = [name for name in names if name]
    return "\n".join(names) + ("\n" if names else "")


def _format_timestamp(value: Any) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    while stamp > 1e11:
        stamp /= 1000.0
    try:
        return datetime.fromtimestamp(stamp).strftime("%d.%m.%Y %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


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


_AUTH_UNSET = object()

_UNAUTHORIZED_PAGE = (
    "<!doctype html><html lang='de'><meta charset='utf-8'>"
    "<meta name='color-scheme' content='dark'>"
    "<title>401 · Nicht autorisiert</title>"
    "<body style='margin:0;height:100vh;display:grid;place-items:center;"
    "background:#05070c;color:#e8edf5;"
    "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif'>"
    "<div style='max-width:32rem;padding:24px;text-align:center'>"
    "<h1 style='margin:0 0 12px'>401 – Nicht autorisiert</h1>"
    "<p style='color:#8b96a8'>{detail}</p>"
    "<p style='color:#8b96a8'>Bitte einen gültigen Bearer-Token im "
    "Authorization-Header senden.</p>"
    "</div></body></html>"
)


def _unauthorized(message: str, as_json: bool) -> Response:
    if as_json:
        response: Response = jsonify(
            ok=False, error="unauthorized", detail=message
        )
    else:
        response = Response(
            _UNAUTHORIZED_PAGE.format(detail=escape(message)),
            status=401,
            mimetype="text/html",
        )
    response.status_code = 401
    response.headers["WWW-Authenticate"] = (
        'Bearer realm="whatsapp-agent", error="invalid_token"'
    )
    return response


def _login_error(message: str, status: int = 401) -> Response:
    page = (
        "<!doctype html><html lang='de'><meta charset='utf-8'>"
        "<meta name='color-scheme' content='dark'>"
        "<title>Login fehlgeschlagen</title>"
        "<body style='margin:0;height:100vh;display:grid;place-items:center;"
        "background:#05070c;color:#e8edf5;"
        "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif'>"
        "<div style='max-width:36rem;padding:24px;text-align:center'>"
        "<h1 style='margin:0 0 12px'>Login fehlgeschlagen</h1>"
        f"<p style='color:#8b96a8'>{escape(message)}</p>"
        f"<p><a style='color:#7fb0ff' href=\"{url_for('login')}\">"
        "Erneut anmelden</a></p>"
        "</div></body></html>"
    )
    return Response(page, status=status, mimetype="text/html")


def _external_base(auth: Any) -> str:
    """External scheme://host, using OIDC_REDIRECT_URI when available.

    Flask sees the request over HTTP behind the TLS reverse proxy, so
    ``url_for(..., _external=True)`` would wrongly produce ``http://`` URLs.
    """
    config = getattr(auth, "config", None)
    redirect_uri = getattr(config, "redirect_uri", "") if config else ""
    if redirect_uri:
        parts = urlsplit(redirect_uri)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return request.url_root.rstrip("/")


def _safe_next(value: str | None) -> str:
    """Only allow redirects to local paths."""
    if not value:
        return ""
    candidate = value.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return ""
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return ""
    return candidate


def create_app(
    service: Any,
    ctx: dict[str, Any],
    auth: Any = _AUTH_UNSET,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SESSION_SECRET") or os.urandom(32)
    app.config["CTX"] = ctx
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get(
            "SESSION_COOKIE_SECURE", ""
        ).strip().lower()
        in {"1", "true", "yes", "on"},
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    if auth is _AUTH_UNSET:
        auth = build_auth_from_env()
    app.config["AUTH"] = auth

    @app.context_processor
    def _inject_auth() -> dict[str, Any]:
        return {
            "auth_enabled": auth is not None,
            "current_user": session.get("user"),
        }

    auth_exempt = {
        "/health",
        "/login",
        "/auth/callback",
        "/logout",
    }

    if auth is not None:

        @app.before_request
        def _require_auth() -> Any:
            path = request.path
            if path in auth_exempt or path.startswith("/static/"):
                return None
            is_api = path.startswith("/api/")
            try:
                if is_api:
                    g.auth_claims = auth.check(request.headers)
                else:
                    g.auth_claims = auth.authorize(
                        request.headers, session.get("user")
                    )
            except MissingToken as exc:
                if is_api:
                    return _unauthorized(str(exc), True)
                return redirect(
                    url_for(
                        "login",
                        next=request.full_path.rstrip("?"),
                    )
                )
            except InvalidToken as exc:
                log.warning("rejected token: %s", exc)
                session.pop("user", None)
                if is_api:
                    return _unauthorized("invalid bearer token", True)
                return redirect(
                    url_for(
                        "login",
                        next=request.full_path.rstrip("?"),
                    )
                )
            return None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/group-check")
    def group_check() -> Any:
        checker = _ctx().get("group_check")
        query = request.args.get("group", "")
        if checker is None:
            return jsonify(ok=False, available=False, exists=False)
        if not service.connected.is_set():
            return jsonify(ok=True, available=False, exists=False)
        try:
            result = checker(query)
        except Exception as exc:
            log.exception("group check failed")
            return jsonify(
                ok=False,
                available=False,
                exists=False,
                error=str(exc),
            )
        return jsonify(ok=True, **result)

    @app.get("/login")
    def login() -> Any:
        if auth is None:
            return redirect(url_for("index"))
        next_url = _safe_next(request.args.get("next"))
        if session.get("user"):
            return redirect(next_url or url_for("index"))
        if not auth.can_login:
            abort(
                500,
                "OIDC login is not configured; set OIDC_ISSUER, "
                "OIDC_CLIENT_ID and OIDC_REDIRECT_URI",
            )

        redirect_uri = (
            auth.config.redirect_uri
            or _external_base(auth) + url_for("auth_callback")
        )
        verifier, challenge = new_pkce_pair()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        session["_oidc_state"] = state
        session["_oidc_nonce"] = nonce
        session["_oidc_verifier"] = verifier
        session["_oidc_redirect_uri"] = redirect_uri
        session["_oidc_next"] = next_url
        try:
            target = auth.login.authorization_url(
                state=state,
                nonce=nonce,
                code_challenge=challenge,
                redirect_uri=redirect_uri,
            )
        except Exception as exc:
            log.exception("could not build the OIDC login URL")
            return _unauthorized(f"OIDC login failed: {exc}", False)
        return redirect(target)

    @app.get("/auth/callback")
    def auth_callback() -> Any:
        if auth is None:
            return redirect(url_for("index"))

        error = request.args.get("error")
        if error:
            return _login_error(
                request.args.get("error_description", error)
            )

        expected_state = session.pop("_oidc_state", "")
        state = request.args.get("state", "")
        if not expected_state or state != expected_state:
            return _login_error(
                "Ungültiger Login-Status. Bitte erneut anmelden."
            )

        code = request.args.get("code", "")
        verifier = session.pop("_oidc_verifier", "")
        redirect_uri = session.pop("_oidc_redirect_uri", "")
        nonce = session.pop("_oidc_nonce", "")
        next_url = _safe_next(session.pop("_oidc_next", ""))
        if not code or not verifier:
            return _login_error(
                "Login-Code fehlt. Bitte erneut anmelden."
            )

        try:
            tokens = auth.login.exchange_code(
                code=code,
                code_verifier=verifier,
                redirect_uri=redirect_uri,
            )
            id_token = tokens.get("id_token", "")
            if not id_token:
                raise InvalidToken("token response has no id_token")
            claims = auth.login.validate_id_token(id_token, nonce)
            info = auth.login.userinfo(tokens.get("access_token", ""))
            for field in ("name", "preferred_username", "email", "groups"):
                if not claims.get(field) and info.get(field):
                    claims[field] = info[field]
        except Exception as exc:
            log.exception("OIDC login failed")
            return _login_error(f"Login fehlgeschlagen: {exc}")

        session["user"] = {
            "sub": claims.get("sub", ""),
            "name": (
                claims.get("name")
                or claims.get("preferred_username")
                or claims.get("email")
                or claims.get("sub", "")
            ),
            "email": claims.get("email", ""),
            "groups": claims.get("groups", []),
            "exp": claims.get("exp", int(time.time()) + 3600),
        }
        session["_oidc_id_token"] = id_token
        return redirect(next_url or url_for("index"))

    @app.get("/logout")
    def logout() -> Any:
        id_token = session.pop("_oidc_id_token", "")
        session.pop("user", None)
        for key in (
            "_oidc_state",
            "_oidc_nonce",
            "_oidc_verifier",
            "_oidc_redirect_uri",
            "_oidc_next",
        ):
            session.pop(key, None)
        if auth is not None:
            login_url = _external_base(auth) + url_for("login")
            target = auth.config.logout_url
            if not target:
                try:
                    target = auth.login.logout_url(
                        id_token=id_token,
                        post_logout_redirect_uri=login_url,
                    )
                except Exception:
                    target = ""
            if not target and auth.config.issuer:
                # Authelia-style portals expose their logout page at /logout
                # and accept ?rd= to redirect back to the app afterwards.
                target = (
                    f"{auth.config.issuer}/logout?rd="
                    + quote(login_url, safe="")
                )
            if target:
                return redirect(target)
        return redirect(url_for("login"))

    @app.get("/")
    def index() -> str:
        ctx = _ctx()
        schedule, schedule_error = _schedule_or_default(ctx)
        now = datetime.now(ZoneInfo(schedule["timezone"]))
        _text, polls, polls_error = _read_polls(ctx)
        next_poll = None
        next_poll_index = None
        if polls and ctx.get("choose_poll"):
            try:
                poll_state = ctx["load_poll_state"](ctx["poll_state_path"])
                next_poll_index, next_poll = ctx["choose_poll"](
                    polls, poll_state
                )
            except Exception:
                log.exception("could not determine next poll")
        return render_template(
            "index.html",
            active="dashboard",
            status=service.status(),
            schedule=schedule,
            schedule_error=schedule_error,
            polls_error=polls_error,
            poll_count=len(polls),
            next_poll=next_poll,
            next_poll_index=next_poll_index,
            next_poll_total=len(polls),
            next_slots=ctx["next_schedule_slots"](schedule, now, 5),
            sched_state=ctx["load_schedule_state"](ctx["schedule_state_path"]),
            weekdays=ctx["weekdays"],
            has_simulated=any(
                poll.get("simulated") for poll in _load_history(ctx)
            ),
        )

    @app.post("/simulate")
    def simulate_poll() -> Any:
        ctx = _ctx()
        store = ctx.get("poll_history_store")
        if store is None:
            flash("Simulation ist nicht verfügbar.", "error")
            return redirect(url_for("index"))

        _text, polls, error = _read_polls(ctx)
        if error or not polls:
            polls = [SIMULATION_FALLBACK_POLL]
        source = random.choice(polls)
        options = source["options"]

        message_id = f"sim-{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        group = "Simulation"
        store.record_sent_poll(
            message_id=message_id,
            chat="simulation@g.us",
            group_name=group,
            question=source["question"],
            options=options,
            timestamp=now,
            simulated=True,
        )

        count = random.randint(6, min(12, len(SIMULATION_PEOPLE)))
        for number, name in random.sample(SIMULATION_PEOPLE, count):
            choice = random.choice(options)
            store.record_vote(
                poll_id=message_id,
                chat="simulation@g.us",
                sender=f"{number}@s.whatsapp.net",
                sender_alt="",
                pushname=name,
                selected_hashes=[
                    hashlib.sha256(choice.encode("utf-8")).hexdigest()
                ],
                timestamp=now,
            )

        flash(
            f"Simulierte Umfrage mit {count} zufälligen Antworten erstellt.",
            "success",
        )
        return redirect(url_for("votes_page", i=0))

    @app.post("/simulate/clear")
    def clear_simulation() -> Any:
        store = _ctx().get("poll_history_store")
        if store is not None:
            removed = store.clear_simulated()
            flash(
                f"{removed} simulierte Umfrage(n) gelöscht.",
                "success" if removed else "error",
            )
        return redirect(url_for("index"))

    def _votes_context(
        index_value: str | None, answer_value: str | None
    ) -> dict[str, Any]:
        ctx = _ctx()
        polls = _load_history(ctx)
        contacts = _load_contact_map(ctx)
        try:
            index = int(index_value or "0")
        except ValueError:
            index = 0
        if index < 0:
            index = 0
        if polls and index >= len(polls):
            index = len(polls) - 1
        if not polls:
            index = 0
        poll = polls[index] if polls else None
        options = poll.get("options", []) if poll else []

        all_rows = _vote_rows(poll, contacts)
        for row in all_rows:
            row["time"] = _format_timestamp(row["timestamp"])

        try:
            answer_index = (
                int(answer_value) if answer_value not in (None, "") else None
            )
        except (TypeError, ValueError):
            answer_index = None
        if answer_index is not None and not 0 <= answer_index < len(options):
            answer_index = None

        option_counts = {
            option: sum(1 for row in all_rows if option in row["selected"])
            for option in options
        }

        if answer_index is None:
            active_answer = None
            rows = all_rows
        else:
            active_answer = options[answer_index]
            rows = [
                row for row in all_rows if active_answer in row["selected"]
            ]

        return {
            "polls": polls,
            "poll": poll,
            "index": index,
            "total": len(polls),
            "options": options,
            "rows": rows,
            "voter_count": len(all_rows),
            "filtered_count": len(rows),
            "option_counts": option_counts,
            "answer_index": answer_index,
            "active_answer": active_answer,
            "poll_sent": _format_timestamp(poll.get("sent_at"))
            if poll
            else "",
            "names_export": _names_export(rows),
        }

    @app.get("/votes")
    def votes_page() -> str:
        data = _votes_context(request.args.get("i"), request.args.get("a"))
        return render_template(
            "votes.html", active="votes", **data
        )

    @app.get("/contacts")
    def contacts_page() -> str:
        ctx = _ctx()
        polls = _load_history(ctx)
        contacts = _load_contact_map(ctx)
        schedule, _schedule_error = _schedule_or_default(ctx)
        group = (
            schedule.get("group") or ctx.get("default_group", "")
        ).strip()

        whatsapp_names = _whatsapp_names(polls)
        auto_named = 0
        added = 0
        group_error = None
        directory_loader = ctx.get("group_directory") or ctx.get(
            "group_contacts"
        )
        if group and service.connected.is_set() and directory_loader:
            try:
                raw = directory_loader(group)
            except Exception as exc:
                log.exception("could not load group contacts")
                group_error = str(exc)
                raw = {}

            auto: dict[str, str] = {}
            for number, entry in raw.items():
                if isinstance(entry, str):
                    auto[number] = entry
                else:
                    auto[number] = entry.get("name", "")
                    whatsapp = (entry.get("whatsapp") or "").strip()
                    if whatsapp and not whatsapp_names.get(number):
                        whatsapp_names[number] = whatsapp

            changed = False
            for number, name in auto.items():
                if not number:
                    continue
                if number not in contacts:
                    contacts[number] = ""
                    added += 1
                    changed = True
                if name and not contacts.get(number):
                    contacts[number] = name
                    auto_named += 1
                    changed = True

            if changed:
                path = ctx.get("contacts_path")
                saver = ctx.get("save_contacts")
                if path and saver:
                    try:
                        saver(path, contacts)
                    except OSError:
                        log.exception("could not save contacts")
                        group_error = "Zuordnung konnte nicht gespeichert werden."

        numbers = _collect_numbers(polls)
        rows = _contact_rows(contacts, numbers, whatsapp_names)
        return render_template(
            "contacts.html",
            active="contacts",
            contact_rows=rows,
            number_count=len(rows),
            mapped_count=sum(1 for row in rows if row["name"]),
            group=group,
            group_connected=service.connected.is_set(),
            group_error=group_error,
            auto_named=auto_named,
            added=added,
        )

    @app.post("/contacts")
    def save_contacts_page() -> Any:
        ctx = _ctx()
        contacts: dict[str, str] = {}
        numbers = request.form.getlist("number")
        names = request.form.getlist("name")
        if numbers or names:
            for key, name in zip(numbers, names):
                key = key.strip()
                if key:
                    contacts[key] = name.strip()
        else:
            text = request.form.get("contacts", "")
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, name = line.split("=", 1)
                elif "\t" in line:
                    key, name = line.split("\t", 1)
                else:
                    continue
                key = key.strip()
                name = name.strip()
                if key:
                    contacts[key] = name

        path = ctx.get("contacts_path")
        saver = ctx.get("save_contacts")
        if not path or not saver:
            flash("Zuordnung kann nicht gespeichert werden.", "error")
        else:
            try:
                saver(path, contacts)
            except OSError as exc:
                flash(f"Speichern fehlgeschlagen: {exc}", "error")
            else:
                flash("Zuordnung gespeichert.", "success")
        return redirect(url_for("contacts_page"))

    @app.get("/votes/export.csv")
    def votes_export() -> Response:
        data = _votes_context(request.args.get("i"), request.args.get("a"))
        output = "\ufeff" + _names_export(data["rows"])
        return Response(
            output,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=umfrage.csv"
            },
        )

    @app.post("/votes/delete")
    def delete_poll() -> Any:
        ctx = _ctx()
        store = ctx.get("poll_history_store")
        message_id = request.form.get("message_id", "").strip()
        index = request.form.get("i", "0")
        if store is None:
            flash("Löschen ist nicht verfügbar.", "error")
        elif store.delete_poll(message_id):
            flash("Umfrage gelöscht.", "success")
        else:
            flash("Umfrage nicht gefunden.", "error")
        return redirect(url_for("votes_page", i=index))

    @app.post("/votes/clear")
    def clear_polls() -> Any:
        store = _ctx().get("poll_history_store")
        if store is None:
            flash("Löschen ist nicht verfügbar.", "error")
            return redirect(url_for("votes_page"))
        removed = store.clear_all()
        if removed:
            flash(f"{removed} Umfrage(n) gelöscht.", "success")
        else:
            flash("Keine Umfragen zum Löschen vorhanden.", "warning")
        return redirect(url_for("votes_page"))

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

    @app.get("/calendar")
    def calendar_page() -> str:
        ctx = _ctx()
        config = _calendar_config(ctx)
        _text, polls, polls_error = _read_polls(ctx)
        poll_questions = [poll["question"] for poll in polls]
        scheduler = ctx.get("calendar_scheduler")
        preview: list[dict[str, Any]] = []
        preview_error = None
        if scheduler is not None:
            try:
                preview, preview_error = scheduler.preview(limit=12)
            except Exception as exc:
                log.exception("calendar preview failed")
                preview_error = str(exc)
        return render_template(
            "calendar.html",
            active="calendar",
            calendars=config["calendars"],
            calendar_names={
                calendar.get("id"): calendar.get("name")
                for calendar in config["calendars"]
            },
            rules=config["rules"],
            poll_questions=poll_questions,
            polls_error=polls_error,
            preview=preview,
            preview_error=preview_error,
        )

    def _save_calendar(config: dict[str, Any]) -> bool:
        path = _ctx().get("calendar_config_path")
        if not path:
            flash("Kalender-Konfiguration ist nicht verfügbar.", "error")
            return False
        try:
            save_calendar_config(path, config)
        except (OSError, CalendarError) as exc:
            flash(f"Speichern fehlgeschlagen: {exc}", "error")
            return False
        return True

    @app.post("/calendar/add")
    def add_calendar() -> Any:
        name = request.form.get("name", "").strip()
        url = request.form.get("url", "").strip()
        if not name or not url.lower().startswith(("http://", "https://")):
            flash(
                "Name und eine öffentliche http(s)-ICS-URL sind erforderlich.",
                "error",
            )
            return redirect(url_for("calendar_page"))
        config = _calendar_config(_ctx())
        config["calendars"].append(
            {
                "id": new_calendar_id(),
                "name": name,
                "url": url,
                "enabled": request.form.get("enabled") == "on",
            }
        )
        if _save_calendar(config):
            flash("Kalender hinzugefügt.", "success")
        return redirect(url_for("calendar_page"))

    @app.post("/calendar/delete")
    def delete_calendar() -> Any:
        calendar_id = request.form.get("id", "")
        config = _calendar_config(_ctx())
        before = len(config["calendars"])
        config["calendars"] = [
            calendar
            for calendar in config["calendars"]
            if calendar.get("id") != calendar_id
        ]
        config["rules"] = [
            rule
            for rule in config["rules"]
            if rule.get("calendar_id") != calendar_id
        ]
        if len(config["calendars"]) == before:
            flash("Kalender nicht gefunden.", "error")
        elif _save_calendar(config):
            flash("Kalender und zugehörige Regeln gelöscht.", "success")
        return redirect(url_for("calendar_page"))

    @app.post("/calendar/toggle")
    def toggle_calendar() -> Any:
        calendar_id = request.form.get("id", "")
        config = _calendar_config(_ctx())
        for calendar in config["calendars"]:
            if calendar.get("id") == calendar_id:
                calendar["enabled"] = not calendar.get("enabled", True)
                _save_calendar(config)
                break
        return redirect(url_for("calendar_page"))

    @app.post("/calendar/rule/add")
    def add_calendar_rule() -> Any:
        config = _calendar_config(_ctx())
        calendar_id = request.form.get("calendar_id", "")
        if not any(
            calendar.get("id") == calendar_id
            for calendar in config["calendars"]
        ):
            flash("Bitte einen gültigen Kalender auswählen.", "error")
            return redirect(url_for("calendar_page"))

        match = request.form.get("match", "").strip()
        group = request.form.get("group", "").strip()
        if not match or not group:
            flash("Titel-Match und Gruppe sind erforderlich.", "error")
            return redirect(url_for("calendar_page"))

        kind = request.form.get("kind", "poll")
        if kind not in {"poll", "text"}:
            kind = "poll"
        text = request.form.get("text", "").strip()
        if kind == "text" and not text:
            flash("Für Text-Nachrichten wird ein Text benötigt.", "error")
            return redirect(url_for("calendar_page"))

        match_type = request.form.get("match_type", "contains")
        if match_type not in {"contains", "exact", "regex"}:
            match_type = "contains"

        offset = request.form.get("offset", "20h").strip() or "20h"
        try:
            parse_offset(offset)
        except CalendarError as exc:
            flash(str(exc), "error")
            return redirect(url_for("calendar_page"))

        poll_questions = [
            line.strip()
            for line in request.form.get("poll_questions", "").splitlines()
            if line.strip()
        ]
        config["rules"].append(
            {
                "id": new_calendar_id(),
                "calendar_id": calendar_id,
                "match": match,
                "match_type": match_type,
                "group": group,
                "kind": kind,
                "text": text,
                "poll_questions": poll_questions,
                "offset": offset,
                "enabled": request.form.get("enabled") == "on",
            }
        )
        if _save_calendar(config):
            flash("Regel hinzugefügt.", "success")
        return redirect(url_for("calendar_page"))

    @app.post("/calendar/rule/delete")
    def delete_calendar_rule() -> Any:
        rule_id = request.form.get("id", "")
        config = _calendar_config(_ctx())
        before = len(config["rules"])
        config["rules"] = [
            rule for rule in config["rules"] if rule.get("id") != rule_id
        ]
        if len(config["rules"]) == before:
            flash("Regel nicht gefunden.", "error")
        elif _save_calendar(config):
            flash("Regel gelöscht.", "success")
        return redirect(url_for("calendar_page"))

    @app.post("/calendar/rule/toggle")
    def toggle_calendar_rule() -> Any:
        rule_id = request.form.get("id", "")
        config = _calendar_config(_ctx())
        for rule in config["rules"]:
            if rule.get("id") == rule_id:
                rule["enabled"] = not rule.get("enabled", True)
                _save_calendar(config)
                break
        return redirect(url_for("calendar_page"))

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
