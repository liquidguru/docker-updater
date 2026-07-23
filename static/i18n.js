/* docker-updater i18n + dialogs (en / zh-CN) */
(function (global) {
  const MESSAGES = global.I18N_MESSAGES || { en: {}, "zh-CN": {} };

  const LS_MODE = "du_lang_mode"; // auto | en | zh-CN

  function browserLang() {
    const nav = (navigator.language || navigator.userLanguage || "en").toLowerCase();
    return nav.startsWith("zh") ? "zh-CN" : "en";
  }

  function getLangMode() {
    const m = localStorage.getItem(LS_MODE);
    if (m === "en" || m === "zh-CN" || m === "auto") return m;
    return "auto";
  }

  function setLangMode(mode) {
    if (mode === "en" || mode === "zh-CN" || mode === "auto") {
      localStorage.setItem(LS_MODE, mode);
    }
  }

  function resolveLang(mode) {
    mode = mode || getLangMode();
    if (mode === "en" || mode === "zh-CN") return mode;
    return browserLang();
  }

  let currentLang = resolveLang();

  function t(key, vars) {
    const pack = MESSAGES[currentLang] || MESSAGES.en;
    let s = (pack && pack[key]) || (MESSAGES.en && MESSAGES.en[key]) || key;
    if (vars) {
      s = s.replace(/\{(\w+)\}/g, (_, k) =>
        vars[k] !== undefined && vars[k] !== null ? String(vars[k]) : ""
      );
    }
    return s;
  }

  function setLanguage(mode) {
    setLangMode(mode);
    currentLang = resolveLang(mode);
    document.documentElement.lang = currentLang === "zh-CN" ? "zh-CN" : "en";
    applyI18n();
    return currentLang;
  }

  function applyI18n(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (!key) return;
      const val = t(key);
      if (el.dataset.i18nAttr) {
        el.setAttribute(el.dataset.i18nAttr, val);
      } else {
        el.textContent = val;
      }
    });
    root.querySelectorAll("[data-i18n-html]").forEach((el) => {
      el.innerHTML = t(el.getAttribute("data-i18n-html"));
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    root.querySelectorAll("[data-i18n-aria-label]").forEach((el) => {
      el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria-label")));
    });
    const titleEl = document.querySelector("title");
    if (titleEl) titleEl.textContent = t("app.title");
  }

  // ── Custom dialogs ──────────────────────────────────────────────────────
  let _dlgResolve = null;
  let _dlgPreviousFocus = null;

  function ensureDialogDom() {
    if (document.getElementById("app-dialog-overlay")) return;
    const wrap = document.createElement("div");
    wrap.innerHTML = `
<div class="modal-overlay" id="app-dialog-overlay" style="z-index:300">
  <div class="modal" style="width:min(420px,95vw)" role="dialog" aria-modal="true" aria-labelledby="app-dialog-title" aria-describedby="app-dialog-message">
    <div class="modal-header">
      <h3 id="app-dialog-title"></h3>
      <button type="button" class="btn-ghost" id="app-dialog-x" data-i18n-aria-label="btn.close_dialog" aria-label="Close dialog">✕</button>
    </div>
    <div class="modal-body" style="padding:16px 18px">
      <p id="app-dialog-message" style="white-space:pre-wrap;font-size:13px;line-height:1.55;margin:0;color:var(--text)"></p>
    </div>
    <div class="modal-footer" id="app-dialog-footer"></div>
  </div>
</div>`;
    document.body.appendChild(wrap.firstElementChild);
  }

  function dialogFocusable() {
    const ov = document.getElementById("app-dialog-overlay");
    if (!ov) return [];
    return Array.from(ov.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((el) => el.offsetParent !== null);
  }

  function trapDialogFocus(e) {
    if (e.key !== "Tab") return;
    const focusable = dialogFocusable();
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (!document.getElementById("app-dialog-overlay").contains(active)) {
      e.preventDefault();
      first.focus();
    } else if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function closeDialog(result) {
    const ov = document.getElementById("app-dialog-overlay");
    if (ov) ov.classList.remove("open");
    document.removeEventListener("keydown", onDialogKey);
    const previous = _dlgPreviousFocus;
    _dlgPreviousFocus = null;
    const r = _dlgResolve;
    _dlgResolve = null;
    if (r) r(result);
    if (previous && typeof previous.focus === "function" && document.contains(previous)) {
      setTimeout(() => previous.focus(), 0);
    }
  }

  function onDialogKey(e) {
    if (e.key === "Tab") {
      trapDialogFocus(e);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      closeDialog(false);
    } else if (e.key === "Enter") {
      const active = document.activeElement;
      if (active && active.matches("button, a, input, select, textarea")) return;
      e.preventDefault();
      closeDialog(true);
    }
  }

  function showDialog({ title, message, confirm }) {
    ensureDialogDom();
    return new Promise((resolve) => {
      _dlgPreviousFocus = document.activeElement;
      _dlgResolve = resolve;
      document.getElementById("app-dialog-title").textContent =
        title || (confirm ? t("modal.dialog_title") : t("modal.alert_title"));
      document.getElementById("app-dialog-message").textContent = message || "";
      const foot = document.getElementById("app-dialog-footer");
      if (confirm) {
        foot.innerHTML =
          `<button type="button" class="btn-secondary btn-sm" id="app-dialog-cancel">${t("btn.cancel")}</button>` +
          `<button type="button" class="btn-primary btn-sm" id="app-dialog-ok">${t("btn.confirm")}</button>`;
      } else {
        foot.innerHTML = `<button type="button" class="btn-primary btn-sm" id="app-dialog-ok">${t("btn.ok")}</button>`;
      }
      const ov = document.getElementById("app-dialog-overlay");
      ov.classList.add("open");
      document.getElementById("app-dialog-x").setAttribute("aria-label", t("btn.close_dialog"));
      document.getElementById("app-dialog-ok").onclick = () => closeDialog(true);
      const cancel = document.getElementById("app-dialog-cancel");
      if (cancel) cancel.onclick = () => closeDialog(false);
      document.getElementById("app-dialog-x").onclick = () => closeDialog(false);
      ov.onclick = (e) => {
        if (e.target === ov) closeDialog(false);
      };
      document.removeEventListener("keydown", onDialogKey);
      document.addEventListener("keydown", onDialogKey);
      setTimeout(() => document.getElementById("app-dialog-ok")?.focus(), 0);
    });
  }

  function showAlert(message, title) {
    return showDialog({ title, message, confirm: false }).then(() => undefined);
  }

  function showConfirm(message, title) {
    return showDialog({ title, message, confirm: true });
  }

  // init language from storage immediately
  currentLang = resolveLang();
  if (document.documentElement) {
    document.documentElement.lang = currentLang === "zh-CN" ? "zh-CN" : "en";
  }

  global.I18N = {
    t,
    applyI18n,
    setLanguage,
    getLangMode,
    resolveLang,
    browserLang,
    get currentLang() {
      return currentLang;
    },
  };
  global.t = t;
  global.showAlert = showAlert;
  global.showConfirm = showConfirm;
})(window);
