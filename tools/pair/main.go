// Command pair links a passkey-locked WhatsApp account to a fresh whatsmeow
// device store. The WebAuthn assertion is delegated to the account owner's
// browser (see docs/passkey-pairing.md).
package main

import (
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

//go:embed index.html
var indexHTML []byte

type pairApp struct {
	mu     sync.Mutex
	client *whatsmeow.Client
	token  string
	code   string
	skip   bool
	paired bool
	conn   bool
	fail   string
}

func (a *pairApp) setError(err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.fail = err.Error()
}

func (a *pairApp) authorized(r *http.Request) bool {
	if a.token == "" {
		return true
	}
	return r.URL.Query().Get("token") == a.token ||
		r.Header.Get("X-Pair-Token") == a.token
}

func randomToken() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		log.Fatalf("random token: %v", err)
	}
	return hex.EncodeToString(buf)
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}

func (a *pairApp) page(w http.ResponseWriter, r *http.Request) {
	if !a.authorized(r) {
		http.Error(w, "forbidden: missing or wrong token", http.StatusForbidden)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(indexHTML)
}

func (a *pairApp) challenge(w http.ResponseWriter, r *http.Request) {
	if !a.authorized(r) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 40*time.Second)
	defer cancel()
	for i := 0; i < 15 && !a.client.IsConnected(); i++ {
		time.Sleep(time.Second)
	}
	pk, err := a.client.DangerousInternals().GetPasskeyRequestOptions(ctx)
	if err != nil {
		a.setError(err)
		http.Error(w, "passkey challenge failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	a.mu.Lock()
	a.fail = ""
	a.mu.Unlock()
	writeJSON(w, map[string]any{"publicKey": pk})
}

func (a *pairApp) response(w http.ResponseWriter, r *http.Request) {
	if !a.authorized(r) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	var assertion types.WebAuthnResponse
	if err := json.NewDecoder(r.Body).Decode(&assertion); err != nil {
		http.Error(w, "invalid assertion: "+err.Error(), http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 40*time.Second)
	defer cancel()
	if err := a.client.SendPasskeyResponse(ctx, &assertion); err != nil {
		a.setError(err)
		http.Error(w, "SendPasskeyResponse failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	a.mu.Lock()
	a.fail = ""
	a.mu.Unlock()
	writeJSON(w, map[string]any{"ok": true})
}

func (a *pairApp) status(w http.ResponseWriter, r *http.Request) {
	if !a.authorized(r) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	writeJSON(w, map[string]any{
		"connected":   a.conn,
		"paired":      a.paired,
		"code":        a.code,
		"skipHandoff": a.skip,
		"error":       a.fail,
	})
}

func main() {
	dbPath := flag.String("db", "data/wa_session_new.db", "whatsmeow SQLite database (use a new file)")
	listen := flag.String("addr", "0.0.0.0:8090", "HTTP listen address")
	tokenFlag := flag.String("token", "", "shared secret for the HTTP API (or WA_PAIR_TOKEN; generated if empty)")
	flag.Parse()

	token := *tokenFlag
	if token == "" {
		token = os.Getenv("WA_PAIR_TOKEN")
	}
	if token == "" {
		token = randomToken()
	}

	ctx := context.Background()
	dbLog := waLog.Stdout("Database", "INFO", true)
	clientLog := waLog.Stdout("Client", "INFO", false)

	container, err := sqlstore.New(
		ctx, "sqlite3", "file:"+*dbPath+"?_foreign_keys=on", dbLog,
	)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer container.Close()

	device := container.NewDevice()
	client := whatsmeow.NewClient(device, clientLog)
	app := &pairApp{client: client, token: token}

	client.AddEventHandler(func(evt any) {
		switch e := evt.(type) {
		case *events.PairPasskeyConfirmation:
			app.mu.Lock()
			app.code = e.Code
			app.skip = e.SkipHandoffUX
			app.mu.Unlock()
			log.Printf("Passkey accepted; code=%s skipHandoffUX=%v", e.Code, e.SkipHandoffUX)
			if err := client.SendPasskeyConfirmation(ctx); err != nil {
				log.Printf("SendPasskeyConfirmation failed: %v", err)
				app.setError(err)
			}
		case *events.PairPasskeyError:
			log.Printf("passkey error: %v (continuation=%v)", e.Error, e.Continuation)
			app.setError(e.Error)
		case *events.PairSuccess:
			log.Printf("Device linked: %v", e.ID)
			app.mu.Lock()
			app.paired = true
			app.mu.Unlock()
		case *events.Connected:
			log.Printf("Connected to WhatsApp")
			app.mu.Lock()
			app.conn = true
			app.paired = true
			app.mu.Unlock()
		case *events.LoggedOut:
			log.Printf("Logged out: %v", e.Reason)
			app.setError(fmt.Errorf("logged out: %v", e.Reason))
		}
	})

	if err := client.Connect(); err != nil {
		log.Fatalf("connect: %v", err)
	}
	for i := 0; i < 30 && !client.IsConnected(); i++ {
		time.Sleep(time.Second)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", app.page)
	mux.HandleFunc("/challenge", app.challenge)
	mux.HandleFunc("/response", app.response)
	mux.HandleFunc("/status", app.status)
	server := &http.Server{Addr: *listen, Handler: mux}

	go func() {
		log.Printf("Pairing UI: http://%s/?token=%s", *listen, app.token)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Printf("Shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = server.Shutdown(shutdownCtx)
	client.Disconnect()
}
