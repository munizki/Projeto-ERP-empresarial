document.addEventListener("DOMContentLoaded", function () {
  function appViewportWidth() {
    return window.visualViewport && window.visualViewport.width ? window.visualViewport.width : window.innerWidth;
  }

  function updateVisualViewportWidth() {
    document.documentElement.style.setProperty("--app-visual-width", Math.floor(appViewportWidth()) + "px");
  }

  updateVisualViewportWidth();
  window.addEventListener("resize", updateVisualViewportWidth);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateVisualViewportWidth);
  }

  function readCookie(name) {
    const prefix = name + "=";
    const entries = document.cookie ? document.cookie.split(";") : [];
    for (let index = 0; index < entries.length; index += 1) {
      const cookie = entries[index].trim();
      if (cookie.indexOf(prefix) === 0) {
        return decodeURIComponent(cookie.substring(prefix.length));
      }
    }
    return "";
  }

  function getToastStack() {
    let stack = document.querySelector(".toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      stack.setAttribute("aria-live", "polite");
      stack.setAttribute("aria-atomic", "true");
      document.body.appendChild(stack);
    }
    return stack;
  }

  function resolveToastType(element) {
    if (element.classList.contains("alert-success")) return "success";
    if (element.classList.contains("alert-warning")) return "warning";
    if (element.classList.contains("alert-danger")) return "danger";
    return "info";
  }

  function buildToast(message, type, duration) {
    const icons = {
      success: "OK",
      warning: "!",
      danger: "x",
      info: "i",
    };

    const toast = document.createElement("div");
    toast.className = "toast toast-" + type;
    toast.setAttribute("role", type === "danger" || type === "warning" ? "alert" : "status");
    toast.innerHTML =
      '<div class="toast-icon">' + icons[type] + '</div>' +
      '<div class="toast-body">' + message + '</div>' +
      '<button type="button" class="toast-close" aria-label="Fechar">x</button>';

    function closeToast() {
      toast.classList.remove("is-visible");
      setTimeout(function () {
        if (toast.parentNode) {
          toast.parentNode.removeChild(toast);
        }
      }, 180);
    }

    toast.querySelector(".toast-close").addEventListener("click", closeToast);
    getToastStack().appendChild(toast);
    requestAnimationFrame(function () {
      toast.classList.add("is-visible");
    });

    if (duration > 0) {
      setTimeout(closeToast, duration);
    }

    return toast;
  }

  window.showToast = function (message, type, options) {
    const config = options || {};
    buildToast(message, type || "info", config.duration || 0);
  };

  document.querySelectorAll(".alert").forEach(function (alerta) {
    const type = resolveToastType(alerta);
    const duration = type === "success" || type === "info" ? 4800 : 0;
    buildToast(alerta.innerHTML, type, duration);
    alerta.remove();
  });

  const csrfCookieMeta = document.querySelector('meta[name="csrf-cookie-name"]');
  const csrfCookieName = csrfCookieMeta ? (csrfCookieMeta.getAttribute("content") || "csrf_token") : "csrf_token";
  const csrfToken = readCookie(csrfCookieName);
  if (csrfToken) {
    document.querySelectorAll('form[method="post"], form[method="POST"]').forEach(function (form) {
      let field = form.querySelector('input[name="csrf_token"]');
      if (!field) {
        field = document.createElement("input");
        field.type = "hidden";
        field.name = "csrf_token";
        form.appendChild(field);
      }
      field.value = csrfToken;
    });
  }

  const sessionRoleMeta = document.querySelector('meta[name="session-user-role"]');
  const sessionTimeoutMeta = document.querySelector('meta[name="session-idle-timeout-seconds"]');
  const sessionPingMeta = document.querySelector('meta[name="session-ping-url"]');
  const sessionRole = sessionRoleMeta ? (sessionRoleMeta.getAttribute("content") || "") : "";
  const sessionTimeoutSeconds = sessionTimeoutMeta ? Number(sessionTimeoutMeta.getAttribute("content") || "0") : 0;
  if (sessionRole && sessionRole !== "admin" && sessionTimeoutSeconds > 0) {
    const sessionTimeoutMs = sessionTimeoutSeconds * 1000;
    const sessionPingUrl = sessionPingMeta ? (sessionPingMeta.getAttribute("content") || "/auth/session-ping") : "/auth/session-ping";
    const sessionPingIntervalMs = 60000;
    let lastSessionActivityAt = Date.now();
    let lastSessionPingAt = 0;
    let sessionExpired = false;
    let idleTimer = null;

    function expireSessionByIdle() {
      if (sessionExpired) return;
      sessionExpired = true;
      window.location.href = "/auth/logout?motivo=inatividade";
    }

    function sessionIdleTime() {
      return Date.now() - lastSessionActivityAt;
    }

    function scheduleIdleCheck() {
      if (idleTimer) {
        window.clearTimeout(idleTimer);
      }
      const remainingMs = Math.max(sessionTimeoutMs - sessionIdleTime(), 1);
      idleTimer = window.setTimeout(function () {
        if (sessionIdleTime() >= sessionTimeoutMs) {
          expireSessionByIdle();
          return;
        }
        scheduleIdleCheck();
      }, Math.min(remainingMs, 60000));
    }

    function pingActiveSession() {
      if (sessionExpired) return;
      if (sessionIdleTime() >= sessionTimeoutMs) {
        expireSessionByIdle();
        return;
      }
      const now = Date.now();
      if (now - lastSessionPingAt < sessionPingIntervalMs) return;
      lastSessionPingAt = now;

      window.fetch(sessionPingUrl, {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }).then(function (response) {
        if (response.redirected && response.url) {
          window.location.href = response.url;
          return;
        }
        if (response.status === 401 || response.status === 403) {
          window.location.href = "/auth/login?expirou=1";
        }
      }).catch(function () {
        // Network loss should not reset the local inactivity clock.
      });
    }

    function recordSessionActivity() {
      if (sessionExpired) return;
      if (sessionIdleTime() >= sessionTimeoutMs) {
        expireSessionByIdle();
        return;
      }
      lastSessionActivityAt = Date.now();
      scheduleIdleCheck();
      pingActiveSession();
    }

    ["click", "keydown", "mousemove", "scroll", "touchstart", "input"].forEach(function (eventName) {
      document.addEventListener(eventName, recordSessionActivity, { passive: true });
    });
    window.addEventListener("focus", recordSessionActivity);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        recordSessionActivity();
      }
    });

    scheduleIdleCheck();
    pingActiveSession();
  }

  const menuToggle = document.getElementById("menu-toggle");
  const sidebar = document.getElementById("sidebar");
  const sidebarNav = document.querySelector(".sidebar-nav");
  if (sidebarNav) {
    const sidebarScrollKey = "gmf.sidebar.navScrollTop";

    function readSidebarScroll() {
      try {
        return window.sessionStorage.getItem(sidebarScrollKey);
      } catch (error) {
        return null;
      }
    }

    function saveSidebarScroll() {
      try {
        window.sessionStorage.setItem(sidebarScrollKey, String(sidebarNav.scrollTop || 0));
      } catch (error) {
        // sessionStorage may be disabled by browser policy.
      }
    }

    const savedSidebarScroll = readSidebarScroll();
    const activeSidebarLink = sidebarNav.querySelector(".nav-link.active");
    if (savedSidebarScroll !== null && savedSidebarScroll !== "") {
      sidebarNav.scrollTop = Number(savedSidebarScroll) || 0;
    } else if (activeSidebarLink) {
      requestAnimationFrame(function () {
        activeSidebarLink.scrollIntoView({ block: "nearest" });
      });
    }

    let pendingSidebarSave = false;
    sidebarNav.addEventListener("scroll", function () {
      if (pendingSidebarSave) return;
      pendingSidebarSave = true;
      requestAnimationFrame(function () {
        pendingSidebarSave = false;
        saveSidebarScroll();
      });
    });

    sidebarNav.querySelectorAll("a.nav-link[href]").forEach(function (link) {
      link.addEventListener("click", saveSidebarScroll);
    });
    window.addEventListener("beforeunload", saveSidebarScroll);
  }

  if (menuToggle && sidebar) {
    const mobileMenuBreakpoint = 1000;
    menuToggle.style.display = appViewportWidth() <= mobileMenuBreakpoint ? "block" : "none";
    menuToggle.addEventListener("click", function (event) {
      event.stopPropagation();
      sidebar.classList.toggle("open");
    });
    document.addEventListener("click", function (event) {
      if (appViewportWidth() > mobileMenuBreakpoint) return;
      if (!sidebar.contains(event.target) && event.target !== menuToggle) {
        sidebar.classList.remove("open");
      }
    });
    window.addEventListener("resize", function () {
      menuToggle.style.display = appViewportWidth() <= mobileMenuBreakpoint ? "block" : "none";
      if (appViewportWidth() > mobileMenuBreakpoint) {
        sidebar.classList.remove("open");
      }
    });
  }

  const campoCpf = document.querySelector('input[name="cpf"]');
  if (campoCpf) {
    campoCpf.addEventListener("input", function () {
      let valor = this.value.replace(/\D/g, "");
      valor = valor.replace(/(\d{3})(\d)/, "$1.$2");
      valor = valor.replace(/(\d{3})(\d)/, "$1.$2");
      valor = valor.replace(/(\d{3})(\d{1,2})$/, "$1-$2");
      this.value = valor;
    });
  }

  function ensureConfirmModal() {
    let backdrop = document.querySelector(".confirm-modal-backdrop");
    if (backdrop) return backdrop;
    backdrop = document.createElement("div");
    backdrop.className = "confirm-modal-backdrop";
    backdrop.innerHTML =
      '<div class="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-modal-title">' +
      '<div class="confirm-modal-head">' +
      '<div class="confirm-modal-icon" aria-hidden="true">!</div>' +
      '<div class="confirm-modal-title" id="confirm-modal-title">Confirmar acao</div>' +
      '</div>' +
      '<div class="confirm-modal-body" id="confirm-modal-message"></div>' +
      '<div class="confirm-modal-actions">' +
      '<button type="button" class="btn btn-secondary" data-confirm-cancel>Cancelar</button>' +
      '<button type="button" class="btn btn-danger" data-confirm-ok>Confirmar</button>' +
      '</div>' +
      '</div>';
    document.body.appendChild(backdrop);
    return backdrop;
  }

  function openConfirmModal(options) {
    const backdrop = ensureConfirmModal();
    const title = backdrop.querySelector(".confirm-modal-title");
    const message = backdrop.querySelector(".confirm-modal-body");
    const ok = backdrop.querySelector("[data-confirm-ok]");
    const cancel = backdrop.querySelector("[data-confirm-cancel]");
    const config = options || {};
    title.textContent = config.title || "Confirmar acao";
    message.textContent = config.message || "Deseja continuar?";
    ok.textContent = config.okLabel || "Confirmar";
    cancel.textContent = config.cancelLabel || "Cancelar";
    backdrop.classList.add("is-visible");
    ok.focus();

    function close() {
      backdrop.classList.remove("is-visible");
      ok.removeEventListener("click", confirm);
      cancel.removeEventListener("click", cancelAction);
      backdrop.removeEventListener("click", backdropClick);
      document.removeEventListener("keydown", keyHandler);
    }

    function confirm() {
      close();
      if (typeof config.onConfirm === "function") {
        config.onConfirm();
      }
    }

    function cancelAction() {
      close();
      if (typeof config.onCancel === "function") {
        config.onCancel();
      }
    }

    function backdropClick(event) {
      if (event.target === backdrop) {
        cancelAction();
      }
    }

    function keyHandler(event) {
      if (event.key === "Escape") {
        cancelAction();
      }
    }

    ok.addEventListener("click", confirm);
    cancel.addEventListener("click", cancelAction);
    backdrop.addEventListener("click", backdropClick);
    document.addEventListener("keydown", keyHandler);
  }

  document.querySelectorAll("[data-confirm]").forEach(function (elemento) {
    elemento.addEventListener("click", function (event) {
      if (this.dataset.confirmed === "1") {
        delete this.dataset.confirmed;
        return;
      }

      const form = this.form || this.closest("form");
      const type = (this.getAttribute("type") || "").toLowerCase();
      if (form && (type === "submit" || this.tagName === "BUTTON") && !form.reportValidity()) {
        event.preventDefault();
        return;
      }

      event.preventDefault();
      const target = this;
      openConfirmModal({
        title: target.getAttribute("data-confirm-title") || "Confirmar acao",
        message: target.getAttribute("data-confirm") || "Deseja continuar?",
        okLabel: target.getAttribute("data-confirm-ok") || "Confirmar",
        onConfirm: function () {
          if (target.tagName === "A" && target.getAttribute("href")) {
            window.location.href = target.getAttribute("href");
            return;
          }
          if (form && (type === "submit" || target.tagName === "BUTTON")) {
            if (form.requestSubmit) {
              form.requestSubmit(target);
            } else {
              form.submit();
            }
            return;
          }
          target.dataset.confirmed = "1";
          target.click();
        },
      });
    });
  });

  document.querySelectorAll("[data-popup-message]").forEach(function (elemento) {
    elemento.addEventListener("click", function (event) {
      event.preventDefault();
      const mensagem = this.getAttribute("data-popup-message");
      if (!mensagem) return;
      const tipo = this.getAttribute("data-popup-type") || "info";
      buildToast(mensagem, tipo, tipo === "success" || tipo === "info" ? 4800 : 0);
    });
  });

  document.querySelectorAll("input[name='serial_number'], input[name='numero_interno'], input[name='numero_serie'], input[name^='hidrometro_'], input[name='confirmacao_serial'], [data-scanner-field], .confirm-match").forEach(function (campo) {
    campo.addEventListener("input", function () {
      const cursor = this.selectionStart;
      this.value = this.value.toUpperCase();
      if (typeof cursor === "number") {
        this.setSelectionRange(cursor, cursor);
      }
    });
  });

  const primeiroCampoScanner = document.querySelector("[data-scanner-field][autofocus]");
  if (primeiroCampoScanner && document.activeElement === document.body) {
    primeiroCampoScanner.focus();
  }

  document.querySelectorAll("button[data-toggle-detail]").forEach(function (botao) {
    botao.addEventListener("click", function () {
      const alvo = document.getElementById(this.getAttribute("data-toggle-detail"));
      if (!alvo) return;
      alvo.classList.toggle("hidden");
      this.textContent = alvo.classList.contains("hidden") ? "Ver log" : "Ocultar log";
    });
  });

  function refreshConfirmations() {
    let allMatched = true;
    document.querySelectorAll(".confirm-match").forEach(function (campo) {
      const esperado = (campo.dataset.expected || "").trim().toUpperCase();
      const atual = campo.value.trim().toUpperCase();
      if (!atual) {
        campo.style.borderColor = "";
        allMatched = false;
        return;
      }
      const matched = esperado === atual;
      campo.style.borderColor = matched ? "rgba(31, 122, 82, 0.45)" : "rgba(179, 60, 46, 0.45)";
      campo.style.boxShadow = matched ? "0 0 0 4px rgba(31, 122, 82, 0.08)" : "0 0 0 4px rgba(179, 60, 46, 0.08)";
      if (!matched) {
        allMatched = false;
      }
    });

    const botaoEntrega = document.getElementById("btn-entrega");
    if (botaoEntrega) {
      const hasConfirmationFields = document.querySelectorAll(".confirm-match").length > 0;
      if (hasConfirmationFields) {
        botaoEntrega.disabled = !allMatched;
      }
    }
  }

  document.querySelectorAll(".confirm-match").forEach(function (campo) {
    campo.addEventListener("input", refreshConfirmations);
    campo.addEventListener("blur", refreshConfirmations);
  });
  refreshConfirmations();

  const dirtyForms = [];
  document.querySelectorAll("form[data-dirty-check]").forEach(function (form) {
    let dirty = false;
    let submitting = false;

    function setDirty(value) {
      if (submitting) return;
      dirty = value;
      form.dataset.dirty = value ? "1" : "0";
    }

    form.querySelectorAll("input, select, textarea").forEach(function (field) {
      const type = (field.getAttribute("type") || "").toLowerCase();
      if (type === "hidden") return;
      field.addEventListener("input", function () { setDirty(true); });
      field.addEventListener("change", function () { setDirty(true); });
    });

    form.addEventListener("submit", function () {
      submitting = true;
      dirty = false;
      form.dataset.dirty = "0";
    });

    dirtyForms.push(function () { return dirty; });
  });

  function hasDirtyForms() {
    return dirtyForms.some(function (isDirty) { return isDirty(); });
  }

  if (dirtyForms.length) {
    window.addEventListener("beforeunload", function (event) {
      if (!hasDirtyForms()) return;
      event.preventDefault();
      event.returnValue = "";
    });

    document.addEventListener("click", function (event) {
      const link = event.target.closest("a[href]");
      if (!link || !hasDirtyForms()) return;

      const href = link.getAttribute("href") || "";
      if (!href || href.startsWith("#") || link.target === "_blank" || link.hasAttribute("download") || link.dataset.ignoreDirty === "true") {
        return;
      }

      event.preventDefault();
      const mensagem = link.dataset.safeNav || "Existem alteracoes nao salvas nesta tela. Deseja sair mesmo assim?";
      openConfirmModal({
        title: "Sair desta tela?",
        message: mensagem,
        okLabel: "Sair",
        onConfirm: function () {
          window.location.href = href;
        },
      });
    });
  }

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      const submit = form.querySelector('button[type="submit"]');
      if (!submit || submit.disabled) return;
      const original = submit.textContent;
      submit.disabled = true;
      submit.dataset.originalText = original;
      submit.textContent = "Processando...";
      setTimeout(function () {
        if (!document.body.contains(submit)) return;
        submit.disabled = false;
        submit.textContent = submit.dataset.originalText || original;
      }, 8000);
    });
  });
});
