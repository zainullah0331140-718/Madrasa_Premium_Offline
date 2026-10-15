/* Madrasa Premium Offline — tiny dependency-free helpers (no CDN needed). */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var NS = "http://www.w3.org/2000/svg";
  function el(name, attrs, text) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function fmt(n) { return Number(n || 0).toLocaleString("en-US"); }

  /* ---------- mobile menu ---------- */
  var menu = $("#menuBtn"), side = $("#sidebar");
  if (menu && side) {
    menu.addEventListener("click", function () { side.classList.toggle("open"); });
    document.addEventListener("click", function (e) {
      if (side.classList.contains("open") && !side.contains(e.target) && !menu.contains(e.target)) side.classList.remove("open");
    });
  }

  /* ---------- toast auto-hide ---------- */
  $$(".toast").forEach(function (t) { setTimeout(function () { t.style.display = "none"; }, t.classList.contains("err") ? 9000 : 6500); });

  /* ---------- confirm dialog for destructive forms ---------- */
  var dlg = $("#confirmDlg");
  var pending = null;
  $$("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (f.dataset.ok === "1") return;
      e.preventDefault();
      if (dlg && dlg.showModal) {
        pending = f;
        $("#confirmMsg", dlg).textContent = f.dataset.confirm;
        dlg.showModal();
      } else if (window.confirm(f.dataset.confirm)) { f.dataset.ok = "1"; f.submit(); }
    });
  });
  if (dlg) {
    $("#confirmYes", dlg).addEventListener("click", function () {
      dlg.close();
      if (pending) { pending.dataset.ok = "1"; pending.submit(); }
    });
    $("#confirmNo", dlg).addEventListener("click", function () { pending = null; dlg.close(); });
  }

  /* ---------- live table filter ---------- */
  $$("input[data-filter]").forEach(function (inp) {
    inp.addEventListener("input", function () {
      var q = inp.value.trim().toLowerCase();
      $$(inp.dataset.filter + " tbody tr").forEach(function (tr) {
        tr.style.display = !q || tr.textContent.toLowerCase().indexOf(q) > -1 ? "" : "none";
      });
    });
  });

  /* ---------- print buttons ---------- */
  $$("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

  /* ---------- attendance: mark everyone ---------- */
  $$("[data-markall]").forEach(function (b) {
    b.addEventListener("click", function () {
      var v = b.dataset.markall;
      $$('input[type=radio][value="' + v + '"]').forEach(function (r) { r.checked = true; });
    });
  });

  /* ---------- fee form: auto-fill the amount from the student's monthly fee ---------- */
  var feeStudent = $("#feeStudent"), feeAmount = $("#feeAmount"), feeType = $("#feeType");
  function fillFee() {
    if (!feeStudent || !feeAmount) return;
    var opt = feeStudent.options[feeStudent.selectedIndex];
    var fee = opt && opt.dataset.fee;
    if (fee && Number(fee) > 0 && (!feeType || feeType.value === "ماہانہ فیس") && !feeAmount.dataset.touched) feeAmount.value = fee;
  }
  if (feeStudent) {
    feeStudent.addEventListener("change", fillFee);
    if (feeType) feeType.addEventListener("change", fillFee);
    if (feeAmount) feeAmount.addEventListener("input", function () { feeAmount.dataset.touched = "1"; });
  }

  /* ---------- result bulk entry: live percentage ---------- */
  var totalInp = $("#bulkTotal");
  $$("input.marks").forEach(function (inp) {
    function upd() {
      var t = Number(totalInp && totalInp.value), v = inp.value === "" ? null : Number(inp.value);
      var out = inp.closest("tr").querySelector(".live");
      if (!out) return;
      if (v === null || !t) { out.textContent = "—"; inp.style.borderColor = ""; return; }
      var bad = v < 0 || v > t;
      inp.style.borderColor = bad ? "#c0392b" : "";
      out.textContent = bad ? "غلط" : (Math.round(v * 1000 / t) / 10) + "%";
    }
    inp.addEventListener("input", upd);
    if (totalInp) totalInp.addEventListener("input", upd);
    upd();
  });

  /* ---------- charts (inline SVG, no library) ---------- */
  function barChart(host, data) {
    var W = 640, H = 250, pl = 46, pr = 10, pt = 14, pb = 34;
    var max = 0; data.forEach(function (d) { max = Math.max(max, d.income, d.expense); });
    if (!max) { host.innerHTML = '<p class="muted center" style="padding:60px 0">ابھی اس مدت کی آمدن و اخراجات کا ڈیٹا موجود نہیں</p>'; return; }
    var step = Math.pow(10, Math.floor(Math.log10(max))); var top = Math.ceil(max / step) * step;
    if (top / step < 4) { step = step / 2; top = Math.ceil(max / step) * step; }
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });
    for (var g = 0; g <= top; g += step) {
      var y = pt + (H - pt - pb) * (1 - g / top);
      svg.appendChild(el("line", { x1: pl, x2: W - pr, y1: y, y2: y, stroke: "#e4e6ea", "stroke-width": 1 }));
      svg.appendChild(el("text", { x: pl - 6, y: y + 4, "text-anchor": "end", "font-size": 11, fill: "#6b7480" }, fmt(g)));
    }
    var gw = (W - pl - pr) / data.length, bw = Math.min(26, gw / 3);
    data.forEach(function (d, i) {
      var cx = pl + gw * i + gw / 2;
      [[d.income, "#f5b50a", -bw - 2], [d.expense, "#aab0b8", 2]].forEach(function (b) {
        var h = (H - pt - pb) * (b[0] / top);
        var r = el("rect", { x: cx + b[2], y: H - pb - h, width: bw, height: Math.max(h, b[0] ? 2 : 0), rx: 5, fill: b[1] });
        r.appendChild(el("title", {}, fmt(b[0])));
        svg.appendChild(r);
      });
      svg.appendChild(el("text", { x: cx, y: H - 12, "text-anchor": "middle", "font-size": 12, fill: "#3b424b" }, d.label));
    });
    host.innerHTML = ""; host.appendChild(svg);
  }

  function donut(host, data) {
    var total = data.reduce(function (a, d) { return a + d.value; }, 0);
    var size = 170, r = 64, sw = 24, c = 2 * Math.PI * r;
    var svg = el("svg", { viewBox: "0 0 " + size + " " + size, width: size, height: size, role: "img", style: "max-width:170px" });
    svg.appendChild(el("circle", { cx: 85, cy: 85, r: r, fill: "none", stroke: "#eceef1", "stroke-width": sw }));
    var off = 0;
    if (total) data.forEach(function (d) {
      if (!d.value) return;
      var len = c * d.value / total;
      svg.appendChild(el("circle", { cx: 85, cy: 85, r: r, fill: "none", stroke: d.color, "stroke-width": sw,
        "stroke-dasharray": len + " " + (c - len), "stroke-dashoffset": -off, transform: "rotate(-90 85 85)" }));
      off += len;
    });
    var pctTxt = total ? Math.round((data[0].value / total) * 100) + "%" : "—";
    svg.appendChild(el("text", { x: 85, y: 92, "text-anchor": "middle", "font-size": 26, "font-weight": 700, fill: "#1f2328" }, pctTxt));
    var wrap = document.createElement("div"); wrap.className = "donut-wrap"; wrap.appendChild(svg);
    var lg = document.createElement("div"); lg.className = "legend"; lg.style.flexDirection = "column"; lg.style.gap = "6px";
    data.forEach(function (d) {
      var s = document.createElement("div"); s.innerHTML = '<i style="background:' + d.color + '"></i>' + d.label + " — <b>" + d.value + "</b>"; lg.appendChild(s);
    });
    wrap.appendChild(lg); host.innerHTML = ""; host.appendChild(wrap);
  }

  $$(".chart[data-chart]").forEach(function (h) {
    try {
      var data = JSON.parse(h.dataset.chart);
      if (h.dataset.type === "bars") barChart(h, data); else if (h.dataset.type === "donut") donut(h, data);
    } catch (e) { /* ignore malformed chart data */ }
  });
})();
