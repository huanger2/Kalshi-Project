// On a markets page an admin is viewing while a daily run is going: check
// on it every 10 seconds, and reload the page once it has finished, so the
// newly logged games and the run's result show up without a manual refresh.
(function () {
    "use strict";
    var line = document.getElementById("run-status");
    if (!line || line.getAttribute("data-state") !== "running") return;
    var runId = line.getAttribute("data-run");

    function reload() {
        // Drop ?run=started so the "started" message doesn't show again.
        var url = new URL(window.location.href);
        url.searchParams.delete("run");
        window.location.replace(url.toString());
    }

    function check() {
        fetch("/admin/daily-run", { credentials: "same-origin" })
            .then(function (response) { return response.ok ? response.json() : null; })
            .then(function (status) {
                if (status && (status.state !== "running" || String(status.run_id) !== runId)) reload();
                else setTimeout(check, 10000);
            })
            .catch(function () { setTimeout(check, 10000); });
    }

    setTimeout(check, 10000);
})();
