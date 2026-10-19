// The Betting tab's live preview: as the rules form (rules and AI weights)
// changes, ask the server to replay it (POST /betting/preview, nothing saved)
// and swap in the What if? rows and the AI odds' Brier score. Without
// JavaScript the page still works: the numbers just update on save.
(function () {
    "use strict";
    var form = document.getElementById("rules-form");
    var rows = document.getElementById("backtest-rows");
    if (!form) return;
    var status = document.getElementById("preview-status");
    var badge = document.getElementById("preview-badge");
    var brier = document.getElementById("weights-brier");
    var brierGames = document.getElementById("weights-brier-games");

    function serialize() {
        return new URLSearchParams(new FormData(form)).toString();
    }
    var saved = serialize();
    var timer = null;
    var inFlight = null;

    function setStatus(text, isError) {
        if (!status) return;
        status.textContent = text;
        status.classList.toggle("error", !!isError);
    }

    function refresh() {
        var body = serialize();
        var dirty = body !== saved;
        if (badge) badge.hidden = !dirty;
        if (inFlight) inFlight.abort();
        inFlight = new AbortController();
        if (rows) rows.classList.add("loading");
        setStatus("Updating the numbers...");
        fetch("/betting/preview", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: body,
            signal: inFlight.signal,
            credentials: "same-origin"
        }).then(function (response) {
            return response.json().then(function (data) { return { ok: response.ok, data: data }; });
        }).then(function (result) {
            if (rows) rows.classList.remove("loading");
            if (!result.ok) {
                setStatus((result.data.errors || ["Couldn't preview these values."]).join(" "), true);
                return;
            }
            if (rows) rows.innerHTML = result.data.rows_html;  // rendered server-side from our own template
            if (brier) brier.textContent = result.data.brier == null ? "—" : result.data.brier.toFixed(4);
            if (brierGames) brierGames.textContent = result.data.games;
            setStatus(dirty ? "What if? shows these unsaved values." : "");
        }).catch(function (error) {
            if (error.name === "AbortError") return;
            if (rows) rows.classList.remove("loading");
            setStatus("Couldn't update the preview.", true);
        });
    }

    function schedule() {
        clearTimeout(timer);
        timer = setTimeout(refresh, 350);
    }

    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);
    form.addEventListener("submit", function () { saved = serialize(); });
})();
