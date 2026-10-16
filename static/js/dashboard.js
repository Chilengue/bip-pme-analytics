/* ============================================================
   BIP-PME Analytics · dashboard.js
   Renderiza gráficos Plotly com animações, gere filtros,
   menu mobile, modal de recomendações e modal de zoom dos
   gráficos (clique → ecrã cheio → ESC/clique fora).

   Compatível com:
     • window.GRAFICOS = { chave: {data, layout}, ... }
     • window.GRAFICOS = { chave: "<json string>", ... }
     • #dados-graficos com <script type="application/json">
     • #recs-data     com <script type="application/json">

   Debug:
     Adiciona ?debug=1 ao URL para ver logs detalhados.

   ÍNDICE
   ------
   00. Configuração e utilitários
   01. Helpers de figura
   02. Reveal de cards no scroll
   03. Contadores animados dos KPIs
   04. Barras do ranking
   05. Renderização Plotly (com animação de entrada)
   06. Modal de zoom dos gráficos
   07. Redimensionamento responsivo
   08. Filtros de período
   09. Menu lateral (mobile)
   10. Exportação CSV
   11. Atalhos de teclado
   12. Pesquisa em tempo real
   13. Loading state nos filtros
   14. Modal de recomendações
   15. Botão SAIR (confirmação)
   16. Bootstrap
   17. API pública
   ============================================================ */
(function () {
  "use strict";

  // ---------------------------------------------------------------
  // 00. Configuração e utilitários
  // ---------------------------------------------------------------
  const DEBUG = new URLSearchParams(window.location.search).has("debug");
  const dlog  = (...args) => DEBUG && console.log("[dashboard.js]", ...args);

  const PREFERE_MENOS_MOVIMENTO =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const PALETA_ANIM = [
    "#b3bdff", "#7dd3fc", "#a8e6bc", "#ffd39e", "#ffb4ab",
    "#c4b5fd", "#f0abfc", "#5eead4",
  ];

  /** Constrói um URL com os parâmetros alterados, sem perder os restantes. */
  function _withParams(mudancas) {
    const url = new URL(window.location.href);
    Object.entries(mudancas).forEach(([k, v]) => {
      if (v === null || v === undefined) url.searchParams.delete(k);
      else url.searchParams.set(k, v);
    });
    return url.toString();
  }

  // ---------------------------------------------------------------
  // 01. Helpers de figura
  // ---------------------------------------------------------------
  function normalizarFigura(fig) {
    if (fig == null) return null;

    if (typeof fig === "object") {
      if (!fig.data || !Array.isArray(fig.data) || fig.data.length === 0) {
        return null;
      }
      return fig;
    }

    if (typeof fig === "string") {
      try {
        const obj = JSON.parse(fig);
        if (!obj || !Array.isArray(obj.data) || obj.data.length === 0) {
          return null;
        }
        return obj;
      } catch (e) {
        console.warn("[dashboard.js] figura não é JSON válido:", e);
        return null;
      }
    }

    return null;
  }

  function lerGraficos() {
    if (window.GRAFICOS && typeof window.GRAFICOS === "object") {
      dlog("fonte: window.GRAFICOS");
      return window.GRAFICOS;
    }

    const nodo = document.getElementById("dados-graficos");
    if (nodo) {
      try {
        const obj = JSON.parse(nodo.textContent);
        dlog("fonte: #dados-graficos");
        return obj;
      } catch (e) {
        console.error("[dashboard.js] JSON inválido em #dados-graficos:", e);
      }
    }

    dlog("nenhuma fonte de gráficos encontrada.");
    return {};
  }

  // ---------------------------------------------------------------
  // 02. Reveal de cards no scroll
  // ---------------------------------------------------------------
  function prepararReveal() {
    if (PREFERE_MENOS_MOVIMENTO) return;

    const alvos = document.querySelectorAll(
      ".kpi, .card, .destaque-grid > *"
    );
    if (!alvos.length) return;

    alvos.forEach((el, i) => {
      el.style.opacity = "0";
      el.style.transform = "translateY(18px)";
      el.style.transition =
        "opacity .55s cubic-bezier(.2,0,0,1), transform .55s cubic-bezier(.2,0,0,1)";
      el.style.transitionDelay = (i % 8) * 45 + "ms";
    });

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.style.opacity = "1";
            entry.target.style.transform = "translateY(0)";
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.08, rootMargin: "0px 0px -40px 0px" }
    );

    alvos.forEach((el) => io.observe(el));
  }

  // ---------------------------------------------------------------
  // 03. Contadores animados dos KPIs
  // ---------------------------------------------------------------
  function animarContador(el) {
    if (PREFERE_MENOS_MOVIMENTO) return;

    const texto = el.textContent.trim();
    const match = texto.match(/^([^\d\-+]*)(-?[\d.,\s]+)(.*)$/);
    if (!match) return;

    const prefixo = match[1] || "";
    const sufixo  = match[3] || "";
    const bruto   = match[2].replace(/\s/g, "");

    const temVirgula = bruto.includes(",");
    const num = parseFloat(bruto.replace(/\./g, "").replace(",", "."));
    if (!isFinite(num)) return;

    const casas = temVirgula ? (bruto.split(",")[1] || "").length : 0;
    const dur = 900;
    const t0  = performance.now();

    function frame(now) {
      const p = Math.min((now - t0) / dur, 1);
      const eased = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
      const atual = num * eased;

      const fmt = atual.toLocaleString("pt-PT", {
        minimumFractionDigits: casas,
        maximumFractionDigits: casas,
      });
      el.textContent = prefixo + fmt + sufixo;

      if (p < 1) requestAnimationFrame(frame);
      else {
        el.textContent = prefixo +
          num.toLocaleString("pt-PT", {
            minimumFractionDigits: casas,
            maximumFractionDigits: casas,
          }) + sufixo;
      }
    }

    requestAnimationFrame(frame);
  }

  function ativarContadores() {
    document.querySelectorAll(".kpi h3").forEach((el) => {
      if (el.dataset.animado === "1") return;
      el.dataset.animado = "1";
      animarContador(el);
    });
  }

  // ---------------------------------------------------------------
  // 04. Barras do ranking
  // ---------------------------------------------------------------
  function animarRanking() {
    if (PREFERE_MENOS_MOVIMENTO) return;

    document.querySelectorAll(".rank-fill").forEach((el, i) => {
      const alvo = el.style.width || el.getAttribute("data-w") || "0%";
      el.style.width = "0%";
      el.style.transition = "width .9s cubic-bezier(.2,0,0,1)";
      el.style.transitionDelay = i * 60 + "ms";
      requestAnimationFrame(() => {
        el.style.width = alvo;
      });
    });
  }

  // ---------------------------------------------------------------
  // 05. Renderização Plotly (com animação de entrada)
  // ---------------------------------------------------------------
  function garantirPlotly() {
    if (typeof Plotly !== "undefined" && Plotly.newPlot) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      const s = document.createElement("script");
      s.src = "https://cdn.plot.ly/plotly-2.35.2.min.js";
      s.charset = "utf-8";
      s.onload  = () => resolve(true);
      s.onerror = () => resolve(false);
      document.head.appendChild(s);
    });
  }

  function prepararTracosParaAnimacao(data) {
    if (PREFERE_MENOS_MOVIMENTO) return data;

    return data.map((traco, idx) => {
      const t = Object.assign({}, traco);

      if (t.type === "bar") {
        t.marker = Object.assign({}, t.marker || {});
        const corBase =
          t.marker.color ||
          (Array.isArray(t.marker.colors) ? t.marker.colors : null) ||
          PALETA_ANIM[idx % PALETA_ANIM.length];
        t.marker.color = corBase;
        t.marker.opacity = 0;
      }

      if (t.type === "pie") {
        t.marker = Object.assign({}, t.marker || {});
        t.marker.opacity = 0;
      }

      if (
        t.type === "scatter" ||
        t.type === "scattergl" ||
        t.type === "line" ||
        (!t.type && t.mode && t.mode.includes("lines"))
      ) {
        t.line = Object.assign({}, t.line || {});
        t.line.width = 0;
      }

      return t;
    });
  }

  function animarEntradaGrafico(el, data) {
    if (PREFERE_MENOS_MOVIMENTO) return;

    const frames = [];
    const N = 28;
    for (let i = 1; i <= N; i++) {
      const p = i / N;
      const eased = 1 - Math.pow(1 - p, 3);

      const frameData = data.map((t) => {
        const upd = {};
        if (t.type === "bar") upd["marker.opacity"] = eased;
        if (t.type === "pie") upd["marker.opacity"] = eased;
        if (
          t.type === "scatter" ||
          t.type === "scattergl" ||
          t.type === "line" ||
          (!t.type && t.mode && t.mode.includes("lines"))
        ) {
          const alvo =
            (t.line && t.line.width != null) ? t.line.width : 2;
          upd["line.width"] = alvo * eased;
        }
        return upd;
      });

      frames.push({
        name: "f" + i,
        data: frameData,
        traces: data.map((_, k) => k),
      });
    }

    const largurasOriginais = data.map((t) => {
      if (t.type === "bar") return 1;
      if (t.type === "pie") return 1;
      if (
        t.type === "scatter" ||
        t.type === "scattergl" ||
        t.type === "line" ||
        (!t.type && t.mode && t.mode.includes("lines"))
      ) {
        return (t.line && t.line.width != null) ? t.line.width : 2;
      }
      return 1;
    });

    Plotly.addFrames(el, frames).then(() => {
      Plotly.animate(
        el,
        frames.map((f) => f.name),
        {
          frame: { duration: 26, redraw: false },
          transition: { duration: 0 },
          mode: "immediate",
          fromcurrent: true,
        }
      ).then(() => {
        const finalUpd = data.map((t, k) => {
          const u = {};
          if (t.type === "bar")   u["marker.opacity"] = 1;
          if (t.type === "pie")   u["marker.opacity"] = 1;
          if (
            t.type === "scatter" ||
            t.type === "scattergl" ||
            t.type === "line" ||
            (!t.type && t.mode && t.mode.includes("lines"))
          ) {
            u["line.width"] = largurasOriginais[k];
          }
          return u;
        });
        Plotly.restyle(
          el,
          finalUpd.reduce((acc, u) => Object.assign(acc, u), {}),
          data.map((_, k) => k)
        );
      }).catch(() => { /* ignora */ });
    }).catch(() => { /* ignora */ });
  }

  function layoutElegante(layoutBase) {
    return Object.assign(
      {
        responsive: true,
        autosize: true,
        margin: { l: 48, r: 24, t: 40, b: 44 },
        font: { family: "Inter, system-ui, sans-serif", size: 12 },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        hoverlabel: {
          bgcolor: "#1b1f26",
          bordercolor: "#3d4555",
          font: {
            family: "Inter, system-ui, sans-serif",
            size: 12,
            color: "#eef0f5",
          },
        },
        hovermode: "closest",
        transition: {
          duration: PREFERE_MENOS_MOVIMENTO ? 0 : 350,
          easing: "cubic-in-out",
        },
        legend: {
          orientation: "h",
          y: -0.18,
          x: 0,
          font: { size: 11, color: "#c4c9d4" },
          bgcolor: "rgba(0,0,0,0)",
        },
      },
      layoutBase || {}
    );
  }

  function renderPlot(id, figJson) {
    const el = document.getElementById(id);
    if (!el) {
      console.warn('[dashboard.js] renderPlot: <div id="' + id + '"> não existe.');
      return Promise.resolve(false);
    }

    const fig = normalizarFigura(figJson);
    if (!fig) {
      dlog("figura nula para #" + id);
      el.innerHTML = '<p class="empty">Sem dados para mostrar.</p>';
      return Promise.resolve(false);
    }

    return garantirPlotly().then((ok) => {
      if (!ok || typeof Plotly === "undefined" || !Plotly.newPlot) {
        console.error("[dashboard.js] Plotly indisponível em #" + id);
        el.innerHTML = '<p class="empty">Plotly não carregado.</p>';
        return false;
      }

      const layout = layoutElegante(fig.layout);
      const data   = prepararTracosParaAnimacao(fig.data);

      const config = {
        responsive: true,
        displaylogo: false,
        displayModeBar: "hover",
        modeBarButtonsToRemove: [
          "lasso2d", "select2d", "autoScale2d", "toggleSpikelines",
        ],
        toImageButtonOptions: {
          format: "png",
          filename: "bip-pme-" + id,
          height: 600,
          width: 1000,
          scale: 2,
        },
      };

      return Plotly.newPlot(el, data, layout, config)
        .then(() => {
          dlog("gráfico #" + id + " desenhado (" + data.length + " traço(s))");

          animarEntradaGrafico(el, data);

          if (!PREFERE_MENOS_MOVIMENTO) {
            el.style.opacity = "0";
            el.style.transform = "translateY(6px)";
            el.style.transition =
              "opacity .5s cubic-bezier(.2,0,0,1), transform .5s cubic-bezier(.2,0,0,1)";
            requestAnimationFrame(() => {
              el.style.opacity = "1";
              el.style.transform = "translateY(0)";
            });
          }

          // Ativar clique para abrir em ecrã cheio
          ativarCliqueZoom(el, id, data, layout);

          return true;
        })
        .catch((err) => {
          console.error("[dashboard.js] Plotly erro em #" + id, err);
          el.innerHTML = '<p class="empty">Falha ao desenhar gráfico.</p>';
          return false;
        });
    });
  }

  function renderTodosGraficos() {
    const bag = lerGraficos();
    const chaves = Object.keys(bag);
    if (chaves.length === 0) {
      dlog("nenhum gráfico para renderizar.");
      return Promise.resolve();
    }
    dlog("a renderizar " + chaves.length + " gráficos:", chaves);

    return chaves.reduce((cadeia, id, i) => {
      return cadeia.then((res) =>
        new Promise((resolve) => {
          setTimeout(() => {
            renderPlot(id, bag[id]).then((ok) => resolve(res.concat(ok)));
          }, PREFERE_MENOS_MOVIMENTO ? 0 : i * 90);
        })
      );
    }, Promise.resolve([])).then((resultados) => {
      const ok     = resultados.filter(Boolean).length;
      const falhas = resultados.length - ok;
      dlog("concluído: " + ok + " OK, " + falhas + " falha(s).");
      ativarContadores();
      animarRanking();
    });
  }

  // =================================================================
  // 06. ZOOM MODAL DOS GRÁFICOS
  // =================================================================
  const zoomPlotId = "plot-zoom-alvo";
  let zoomPlotCriado = false;
  let ultimoFoco = null;

  /** Garante que o modal de zoom existe no DOM (cria uma única vez). */
  function garantirModalZoom() {
    if (document.getElementById("modal-zoom")) return;

    const overlay = document.createElement("div");
    overlay.id = "modal-zoom";
    overlay.className = "modal-zoom-overlay";
    overlay.hidden = true;
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Gráfico ampliado");

    overlay.innerHTML = `
      <div class="modal-zoom" role="document">
        <div class="modal-zoom-head">
          <h3 id="modal-zoom-titulo">Gráfico</h3>
          <button type="button" class="modal-zoom-fechar"
                  aria-label="Fechar (ESC)">
            <span class="material-symbols-rounded" aria-hidden="true">close</span>
          </button>
        </div>
        <div class="modal-zoom-plot" id="${zoomPlotId}"></div>
      </div>
    `;

    document.body.appendChild(overlay);

    // Fechar com clique no overlay
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) fecharZoom();
    });

    // Fechar com botão ✕
    overlay.querySelector(".modal-zoom-fechar")
      .addEventListener("click", fecharZoom);
  }

  /** Torna o gráfico `el` clicável para abrir em ecrã cheio. */
  function ativarCliqueZoom(el, id, data, layout) {
    if (el.dataset.zoomAtivo === "1") return;
    el.dataset.zoomAtivo = "1";

    // Estilo de affordance
    el.style.cursor = "zoom-in";
    el.setAttribute("title", "Clique para ampliar");
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    el.setAttribute("aria-label",
      "Gráfico " + id + ". Prima Enter para ampliar.");

    const abrir = () => abrirZoom(id, data, layout);

    el.addEventListener("click", abrir);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        abrir();
      }
    });
  }

  /** Abre o modal de zoom com o gráfico clonado. */
  function abrirZoom(id, data, layout) {
    garantirModalZoom();

    const overlay = document.getElementById("modal-zoom");
    const titulo  = document.getElementById("modal-zoom-titulo");
    const alvo    = document.getElementById(zoomPlotId);
    if (!overlay || !titulo || !alvo) return;

    // Título — usa o <h2> do card se existir
    const card = document.getElementById(id)?.closest(".card");
    const h2   = card?.querySelector("h2")?.textContent?.trim();
    titulo.textContent = h2 || ("Gráfico · " + id);

    // Limpar plot anterior
    if (alvo._fullLayout) {
      try { Plotly.purge(alvo); } catch (_) { /* ignora */ }
    }
    alvo.innerHTML = "";

    // Guardar foco para devolver depois
    ultimoFoco = document.activeElement;

    // Abrir primeiro, desenhar depois (para o Plotly medir bem)
    overlay.hidden = false;
    void overlay.offsetWidth;
    overlay.classList.add("is-open");
    document.body.style.overflow = "hidden";

    // Clonar dados sem a animação de entrada (já vem cheio)
    const dataClonada = data.map((t) => {
      const c = JSON.parse(JSON.stringify(t));
      if (c.marker && c.type === "bar")   c.marker.opacity = 1;
      if (c.marker && c.type === "pie")   c.marker.opacity = 1;
      if (c.line)                          c.line.width =
        (c.line.width && c.line.width > 0) ? c.line.width : 2;
      return c;
    });

    // Layout extra para o modal (margens maiores, título maior)
    const layoutModal = Object.assign({}, layout, {
      margin: { l: 70, r: 40, t: 30, b: 70 },
      font: { family: "Inter, system-ui, sans-serif", size: 13 },
      legend: Object.assign({}, layout.legend || {}, {
        orientation: "h",
        y: -0.14,
        x: 0,
        font: { size: 12, color: "#c4c9d4" },
      }),
    });

    const config = {
      responsive: true,
      displaylogo: false,
      displayModeBar: "hover",
      modeBarButtonsToRemove: [
        "lasso2d", "select2d", "autoScale2d", "toggleSpikelines",
      ],
      toImageButtonOptions: {
        format: "png",
        filename: "bip-pme-" + id + "-zoom",
        height: 900,
        width: 1400,
        scale: 2,
      },
    };

    garantirPlotly().then(() => {
      Plotly.newPlot(alvo, dataClonada, layoutModal, config)
        .then(() => {
          // Animação suave de entrada
          if (!PREFERE_MENOS_MOVIMENTO) {
            alvo.style.opacity = "0";
            alvo.style.transform = "translateY(10px)";
            alvo.style.transition =
              "opacity .35s cubic-bezier(.2,0,0,1), transform .35s cubic-bezier(.2,0,0,1)";
            requestAnimationFrame(() => {
              alvo.style.opacity = "1";
              alvo.style.transform = "translateY(0)";
            });
          }
          zoomPlotCriado = true;
          // Foco no botão fechar (acessibilidade)
          overlay.querySelector(".modal-zoom-fechar")?.focus();
        })
        .catch((err) => {
          console.error("[dashboard.js] falha no zoom:", err);
          alvo.innerHTML = '<p class="empty">Falha ao ampliar gráfico.</p>';
        });
    });
  }

  /** Fecha o modal de zoom. */
  function fecharZoom() {
    const overlay = document.getElementById("modal-zoom");
    if (!overlay || overlay.hidden) return;

    overlay.classList.remove("is-open");

    setTimeout(() => {
      overlay.hidden = true;

      // Limpar plot para libertar memória
      const alvo = document.getElementById(zoomPlotId);
      if (alvo && zoomPlotCriado) {
        try { Plotly.purge(alvo); } catch (_) { /* ignora */ }
        alvo.innerHTML = "";
        zoomPlotCriado = false;
      }

      document.body.style.overflow = "";

      // Devolver o foco
      if (ultimoFoco && typeof ultimoFoco.focus === "function") {
        ultimoFoco.focus();
      }
      ultimoFoco = null;
    }, 200);
  }

  // Fecha zoom com ESC (independente do modal de recomendações)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const overlay = document.getElementById("modal-zoom");
      if (overlay && !overlay.hidden) {
        e.stopPropagation();
        fecharZoom();
      }
    }
  });

  // ---------------------------------------------------------------
  // 07. Redimensionamento responsivo
  // ---------------------------------------------------------------
  let resizeTimer = null;
  function _resizeAll() {
    document.querySelectorAll(".js-plotly-plot").forEach((el) => {
      try { Plotly.Plots.resize(el); } catch (_) { /* ignora */ }
    });
  }

  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(_resizeAll, 150);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) setTimeout(_resizeAll, 100);
  });

  // ---------------------------------------------------------------
  // 08. Filtros de período
  // ---------------------------------------------------------------
  function aplicarFiltro(preset) {
    window.location.href = _withParams({
      preset: preset, inicio: null, fim: null,
    });
  }

  function aplicarDatas() {
    const ini = document.getElementById("filtro-inicio")?.value || "";
    const fim = document.getElementById("filtro-fim")?.value || "";

    if (ini && fim && ini > fim) {
      alert("A data inicial não pode ser posterior à data final.");
      return;
    }
    if (!ini && !fim) {
      alert("Preenche pelo menos uma das datas.");
      return;
    }

    window.location.href = _withParams({
      preset: null,
      inicio: ini || null,
      fim:    fim || null,
    });
  }

  function limparFiltro() {
    window.location.href = _withParams({
      inicio: null, fim: null, preset: null,
    });
  }

  // ---------------------------------------------------------------
  // 09. Menu lateral (mobile)
  // ---------------------------------------------------------------
  function toggleSidebar() {
    document.body.classList.toggle("sidebar-open");
  }

  function fecharSidebarSeMobile() {
    if (window.innerWidth < 860) {
      document.body.classList.remove("sidebar-open");
    }
  }

  // ---------------------------------------------------------------
  // 10. Exportação CSV
  // ---------------------------------------------------------------
  function exportarCSV(url) {
    if (url) window.open(url, "_blank");
  }

  // ---------------------------------------------------------------
  // 11. Atalhos de teclado
  // ---------------------------------------------------------------
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") fecharSidebarSeMobile();

    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      const input = document.getElementById("pesquisa-empresa");
      if (input) {
        e.preventDefault();
        input.focus();
      }
    }
  });

  // ---------------------------------------------------------------
  // 12. Pesquisa em tempo real
  // ---------------------------------------------------------------
  function filtrarEmpresas() {
    const termo = (document.getElementById("pesquisa-empresa")?.value || "")
      .toLowerCase()
      .trim();
    document.querySelectorAll(".sidebar nav a").forEach((a) => {
      const txt = a.textContent.toLowerCase();
      a.style.display = !termo || txt.includes(termo) ? "" : "none";
    });
  }

  // ---------------------------------------------------------------
  // 13. Loading state nos filtros
  // ---------------------------------------------------------------
  function bindForms() {
    document.querySelectorAll("form.filtro").forEach((form) => {
      form.addEventListener("submit", () => {
        const btn = form.querySelector("button");
        if (btn) {
          btn.disabled = true;
          btn.textContent = "A carregar…";
        }
      });
    });
  }

  // ---------------------------------------------------------------
  // 14. Modal de recomendações
  // ---------------------------------------------------------------
  let RECS = [];

  function carregarRecs() {
    const nodo = document.getElementById("recs-data");
    if (!nodo) return;
    try {
      RECS = JSON.parse(nodo.textContent) || [];
      dlog("recs carregadas:", RECS.length);
    } catch (e) {
      console.error("[dashboard.js] JSON inválido em #recs-data:", e);
      RECS = [];
    }
  }

  function _fmtValor(v, chave) {
    if (v === null || v === undefined) return "—";

    if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v)) {
      return v.slice(0, 10);
    }

    if (typeof v === "number") {
      const chaveMonetaria = [
        "total", "receita", "price_product",
        "minimum_stock_product", "preco_medio",
      ].includes(chave);
      return chaveMonetaria
        ? v.toLocaleString("pt-PT", {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
          }) + " MZN"
        : v.toLocaleString("pt-PT");
    }

    if (typeof v === "string" && /^-?\d+([.,]\d+)?$/.test(v.replace(",", "."))) {
      const n = parseFloat(v.replace(",", "."));
      const chaveMonetaria = [
        "total", "receita", "price_product",
        "minimum_stock_product", "preco_medio",
      ].includes(chave);
      return chaveMonetaria
        ? n.toLocaleString("pt-PT", {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
          }) + " MZN"
        : n.toLocaleString("pt-PT");
    }

    return String(v);
  }

  function abrirModalRec(idx) {
    const rec = RECS[idx];
    if (!rec) {
      console.warn("[dashboard.js] recomendação inexistente:", idx);
      return;
    }

    const overlay = document.getElementById("modal-rec");
    const titulo  = document.getElementById("modal-rec-titulo");
    const texto   = document.getElementById("modal-rec-texto");
    const tabela  = document.getElementById("modal-rec-tabela");
    if (!overlay || !titulo || !texto || !tabela) {
      console.warn("[dashboard.js] elementos do modal não existem.");
      return;
    }

    titulo.textContent = rec.titulo || "Detalhes";
    texto.textContent  = rec.texto  || "";

    const cols  = rec.colunas || [];
    const itens = rec.itens   || [];

    if (itens.length === 0) {
      tabela.innerHTML = '<p class="empty">Sem linhas para mostrar.</p>';
    } else {
      const thead = "<tr>" +
        cols.map(([, rotulo]) => `<th>${rotulo}</th>`).join("") +
        "</tr>";

      const tbody = itens.map((it, i) => {
        const tds = cols.map(([chave]) => {
          const v   = it[chave];
          const txt = _fmtValor(v, chave);
          const alinhar = (
            typeof v === "number" ||
            (typeof v === "string" && /^-?\d/.test(v))
          ) ? ' class="text-right tabular"' : "";
          return `<td${alinhar}>${txt}</td>`;
        }).join("");
        return `<tr style="animation-delay:${i * 24}ms" class="linha-anim">${tds}</tr>`;
      }).join("");

      tabela.innerHTML =
        `<div class="tabela-wrap">
           <table class="tabela">
             <thead>${thead}</thead>
             <tbody>${tbody}</tbody>
           </table>
         </div>`;
    }

    overlay.hidden = false;
    void overlay.offsetWidth;
    overlay.classList.add("is-open");
    document.body.style.overflow = "hidden";

    overlay.querySelector(".modal-rec-fechar")?.focus();
  }

  function fecharModalRec() {
    const overlay = document.getElementById("modal-rec");
    if (!overlay) return;
    overlay.classList.remove("is-open");

    setTimeout(() => {
      overlay.hidden = true;
      document.body.style.overflow = "";
    }, 180);
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const overlay = document.getElementById("modal-rec");
      if (overlay && !overlay.hidden) fecharModalRec();
    }
  });

  document.addEventListener("click", (e) => {
    const overlay = document.getElementById("modal-rec");
    if (overlay && e.target === overlay) fecharModalRec();
  });

  // ---------------------------------------------------------------
  // 15. Botão SAIR (confirmação antes de terminar sessão)
  // ---------------------------------------------------------------
  function bindBotaoSair() {
    const link = document.getElementById("btn-sair");
    if (!link || link.dataset.bind === "1") return;
    link.dataset.bind = "1";

    link.addEventListener("click", (e) => {
      const nome = link.dataset.userName || window.__userName || "esta conta";
      const ok = window.confirm(
        "Terminar sessão?\n\n" +
        "Vais sair de " + nome + " e serás redirecionado para o login."
      );
      if (!ok) {
        e.preventDefault();
        e.stopPropagation();
        return false;
      }
      // Feedback visual antes de navegar
      link.style.opacity = ".6";
      link.style.pointerEvents = "none";
      const span = link.querySelector("span:not(.material-symbols-rounded)");
      if (span) span.textContent = "A sair…";
      // O browser segue o href="/logout"
    });

    // Atalho: Ctrl+Shift+Q também pede confirmação
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey &&
          e.key.toLowerCase() === "q") {
        e.preventDefault();
        link.click();
      }
    });
  }

  // ---------------------------------------------------------------
  // 16. Bootstrap
  // ---------------------------------------------------------------
  function bootstrap() {
    carregarRecs();
    renderTodosGraficos();
    bindForms();
    bindBotaoSair();

    prepararReveal();

    const input = document.getElementById("pesquisa-empresa");
    if (input) input.addEventListener("input", filtrarEmpresas);

    const burger = document.getElementById("btn-sidebar");
    if (burger) burger.addEventListener("click", toggleSidebar);

    document.querySelector(".content")?.addEventListener("click", () => {
      fecharSidebarSeMobile();
    });

    // Garantir que o modal de zoom existe desde o início
    garantirModalZoom();

    dlog("bootstrap concluído.");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }

  // ---------------------------------------------------------------
  // 17. API pública
  // ---------------------------------------------------------------
  window.renderPlot          = renderPlot;
  window.renderTodosGraficos = renderTodosGraficos;
  window.aplicarFiltro       = aplicarFiltro;
  window.aplicarDatas        = aplicarDatas;
  window.limparFiltro        = limparFiltro;
  window.toggleSidebar       = toggleSidebar;
  window.exportarCSV         = exportarCSV;
  window.filtrarEmpresas     = filtrarEmpresas;
  window.abrirModalRec       = abrirModalRec;
  window.fecharModalRec      = fecharModalRec;
  window.abrirZoomGrafico    = abrirZoom;
  window.fecharZoomGrafico   = fecharZoom;
  window.bindBotaoSair       = bindBotaoSair;
})();