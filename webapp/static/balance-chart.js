// The Betting tab's line chart: profit over time (the default, with a $0
// line; completed bets only) or the Kalshi balance over time. Plain SVG,
// sized to its container and redrawn on resize. Colors come from CSS
// classes (style.css, .line-chart), so the light/dark switch restyles it
// without a redraw.
//
// Data: a <script type="application/json"> named by the figure's
// data-source, holding {"profit": series, "balance": series or null}
// (webapp/betting.py charts()), each series {"kind": "pnl" | "balance",
// "points": [{"x", "y"}]}. x is an ISO date or date-time.
(function () {
    "use strict";
    var SVG = "http://www.w3.org/2000/svg";

    function parseX(x) {
        // "2026-10-14" is a game day: read it as local midnight, not UTC.
        var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(x);
        return m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(x);
    }

    function money(v, signed) {
        var s = Math.abs(v).toLocaleString(undefined, { style: "currency", currency: "USD" });
        if (v < 0) return "-" + s;
        return (signed && v > 0 ? "+" : "") + s;
    }

    function niceStep(span, count) {
        var raw = span / Math.max(count, 1);
        var mag = Math.pow(10, Math.floor(Math.log10(raw)));
        var norm = raw / mag;
        return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    }

    function el(name, attrs, parent) {
        var node = document.createElementNS(SVG, name);
        for (var k in attrs) node.setAttribute(k, attrs[k]);
        if (parent) parent.appendChild(node);
        return node;
    }

    function draw(figure, data) {
        var points = data.points.map(function (p) { return { t: parseX(p.x), y: p.y, x: p.x }; });
        var signed = data.kind === "pnl";
        var narrow = figure.clientWidth < 520;
        var width = figure.clientWidth;
        var height = narrow ? 200 : 240;
        var m = { top: 14, right: narrow ? 64 : 84, bottom: 28, left: narrow ? 52 : 64 };
        var w = Math.max(width - m.left - m.right, 40);
        var h = height - m.top - m.bottom;

        // y: clean ticks around the data; P&L always shows its $0 line.
        var ys = points.map(function (p) { return p.y; });
        var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
        if (signed) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
        if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
        var step = niceStep(hi - lo, narrow ? 3 : 4);
        lo = Math.floor(lo / step) * step;
        hi = Math.ceil(hi / step) * step;
        var yAt = function (v) { return m.top + h - (v - lo) / (hi - lo) * h; };

        // x: time, or evenly spaced for a single point.
        var t0 = points[0].t.getTime(), t1 = points[points.length - 1].t.getTime();
        var xAt = function (t) { return m.left + (t1 === t0 ? w / 2 : (t.getTime() - t0) / (t1 - t0) * w); };

        // A redraw (resize) starts fresh: the old SVG goes, and so does any tooltip it was showing.
        figure.querySelector("svg") && figure.removeChild(figure.querySelector("svg"));
        figure.querySelector(".chart-tooltip").hidden = true;
        var svg = el("svg", { width: width, height: height, role: "img", tabindex: "0",
                              "aria-label": figure.getAttribute("data-label") || "Line chart" });
        figure.insertBefore(svg, figure.firstChild);

        // Gridlines and y tick labels (recessive). Profit always gets its own $0 line, stronger,
        // whatever the ticks: above it the trades are ahead, below it behind.
        if (signed) {
            var zy = Math.round(yAt(0)) + 0.5;
            el("line", { x1: m.left, x2: m.left + w, y1: zy, y2: zy, "class": "zero" }, svg);
        }
        for (var v = lo; v <= hi + step / 2; v += step) {
            var y = Math.round(yAt(v)) + 0.5;
            if (!(signed && Math.abs(v) < step / 1e6)) {
                el("line", { x1: m.left, x2: m.left + w, y1: y, y2: y, "class": "grid" }, svg);
            }
            var label = el("text", { x: m.left - 8, y: y, "class": "tick", "text-anchor": "end",
                                     "dominant-baseline": "middle" }, svg);
            label.textContent = money(Math.abs(v) < step / 1e6 ? 0 : v, signed);
        }

        // x tick labels: a few dates spread across the span, never colliding.
        var fmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
        var labels = Math.max(2, Math.min(points.length, Math.floor(w / 90)));
        var used = {};
        for (var i = 0; i < labels; i++) {
            var t = t1 === t0 ? t0 : t0 + (t1 - t0) * i / (labels - 1);
            var text = fmt.format(new Date(t));
            if (used[text]) continue;
            used[text] = true;
            var anchor = labels === 1 || t1 === t0 ? "middle" : i === 0 ? "start" : i === labels - 1 ? "end" : "middle";
            var tl = el("text", { x: xAt(new Date(t)), y: m.top + h + 18, "class": "tick", "text-anchor": anchor }, svg);
            tl.textContent = text;
        }

        // Area wash to the baseline ($0 for P&L, the axis floor for a balance), then the 2px line.
        var base = yAt(signed ? 0 : lo);
        var line = points.map(function (p, i) { return (i ? "L" : "M") + xAt(p.t).toFixed(1) + "," + yAt(p.y).toFixed(1); }).join("");
        if (points.length > 1) {
            el("path", { d: line + "L" + xAt(points[points.length - 1].t).toFixed(1) + "," + base +
                            "L" + xAt(points[0].t).toFixed(1) + "," + base + "Z", "class": "area" }, svg);
            el("path", { d: line, "class": "line" }, svg);
        }

        // End dot with a surface ring, and the latest value as the one direct label.
        var last = points[points.length - 1];
        el("circle", { cx: xAt(last.t), cy: yAt(last.y), r: 4, "class": "dot" }, svg);
        var end = el("text", { x: xAt(last.t) + 10, y: yAt(last.y), "class": "end-label", "dominant-baseline": "middle" }, svg);
        end.textContent = money(last.y, signed);

        // Hover layer: a crosshair that snaps to the nearest point, plus a tooltip.
        var cross = el("line", { y1: m.top, y2: m.top + h, "class": "crosshair", visibility: "hidden" }, svg);
        var focus = el("circle", { r: 4, "class": "dot", visibility: "hidden" }, svg);
        var tip = figure.querySelector(".chart-tooltip");
        var tipValue = tip.querySelector(".value"), tipLabel = tip.querySelector(".label");
        var full = new Intl.DateTimeFormat(undefined, data.kind === "pnl"
            ? { weekday: "short", month: "short", day: "numeric", year: "numeric" }
            : { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
        var current = -1;

        function show(i) {
            current = i;
            var p = points[i], px = xAt(p.t), py = yAt(p.y);
            cross.setAttribute("x1", px); cross.setAttribute("x2", px);
            cross.setAttribute("visibility", "visible");
            focus.setAttribute("cx", px); focus.setAttribute("cy", py);
            focus.setAttribute("visibility", "visible");
            tipValue.textContent = money(p.y, signed);
            tipLabel.textContent = full.format(p.t);
            tip.hidden = false;
            var left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), width - tip.offsetWidth);
            tip.style.left = left + "px";
            tip.style.top = Math.max(py - tip.offsetHeight - 12, 0) + "px";
        }
        function hide() {
            current = -1;
            cross.setAttribute("visibility", "hidden");
            focus.setAttribute("visibility", "hidden");
            tip.hidden = true;
        }
        function nearest(clientX) {
            var x = clientX - svg.getBoundingClientRect().left, best = 0;
            for (var i = 1; i < points.length; i++) {
                if (Math.abs(xAt(points[i].t) - x) < Math.abs(xAt(points[best].t) - x)) best = i;
            }
            return best;
        }
        svg.addEventListener("pointermove", function (e) { show(nearest(e.clientX)); });
        svg.addEventListener("pointerleave", hide);
        svg.addEventListener("focus", function () { show(points.length - 1); });
        svg.addEventListener("blur", hide);
        svg.addEventListener("keydown", function (e) {
            if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                e.preventDefault();
                var i = current < 0 ? points.length - 1 : current + (e.key === "ArrowLeft" ? -1 : 1);
                show(Math.max(0, Math.min(points.length - 1, i)));
            } else if (e.key === "Escape") {
                hide();
            }
        });
    }

    // The chart block can hold several views ({"profit": ..., "balance": ...}): a switch picks one,
    // remembered per browser; elements marked data-series show only for the view picked.
    var VIEW_KEY = "chartView";

    document.querySelectorAll("figure.line-chart[data-source]").forEach(function (figure) {
        var source = document.getElementById(figure.getAttribute("data-source"));
        var all;
        try { all = JSON.parse(source.textContent); } catch (e) { return; }
        if (all.points) all = { profit: all };  // a single series, not a set of views
        var block = figure.closest(".chart-block") || document;
        var view = figure.getAttribute("data-default") || "profit";
        try {
            var saved = localStorage.getItem(VIEW_KEY);
            if (saved && all[saved]) view = saved;
        } catch (e) {}

        function show() {
            var data = all[view];
            block.querySelectorAll("[data-series]").forEach(function (el) {
                el.hidden = el.getAttribute("data-series") !== view;
            });
            block.querySelectorAll("[data-chart-view]").forEach(function (b) {
                b.setAttribute("aria-pressed", String(b.getAttribute("data-chart-view") === view));
            });
            var has = data && data.points && data.points.length;
            figure.hidden = !has;
            if (has) {
                figure.setAttribute("data-label", (view === "profit" ? "Profit" : "Balance") + " over time, " +
                    data.points.length + " points");
                draw(figure, data);
            }
        }

        block.querySelectorAll("[data-chart-view]").forEach(function (b) {
            b.addEventListener("click", function () {
                view = b.getAttribute("data-chart-view");
                try { localStorage.setItem(VIEW_KEY, view); } catch (e) {}
                show();
            });
        });
        var redraw = show;
        redraw();
        if (window.ResizeObserver) {
            var lastWidth = figure.clientWidth;
            new ResizeObserver(function () {
                if (figure.clientWidth !== lastWidth) { lastWidth = figure.clientWidth; redraw(); }
            }).observe(figure);
        }
    });
})();
