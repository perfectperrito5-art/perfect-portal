(() => {
  // Auto-hide flash banners so they don't stick / look broken on phones
  document.querySelectorAll(".flash-stack .flash").forEach((el) => {
    setTimeout(() => {
      el.classList.add("is-leaving");
      setTimeout(() => el.remove(), 400);
    }, 4500);
  });

  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.tab;
      tabs.forEach((t) => {
        t.classList.toggle("is-active", t === tab);
        t.setAttribute("aria-selected", t === tab ? "true" : "false");
      });
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        const match = panel.id === `tab-${name}`;
        panel.hidden = !match;
        panel.classList.toggle("is-active", match);
      });
    });
  });


  // Token account tabs and Add Tokens dialog.
  const tokenTabs = document.querySelectorAll("[data-token-tab]");
  const tokenPanels = document.querySelectorAll("[data-token-panel]");
  const activateTokenTab = (name) => {
    tokenTabs.forEach((tab) => {
      const active = tab.dataset.tokenTab === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    tokenPanels.forEach((panel) => { panel.hidden = panel.dataset.tokenPanel !== name; });
  };
  tokenTabs.forEach((tab) => tab.addEventListener("click", () => activateTokenTab(tab.dataset.tokenTab)));
  document.querySelectorAll("[data-token-tab-trigger]").forEach((button) => {
    button.addEventListener("click", () => {
      activateTokenTab(button.dataset.tokenTabTrigger);
      document.querySelector("#token-panel-tokens")?.scrollIntoView({ behavior:"smooth", block:"nearest" });
    });
  });
  const supportDialog = document.getElementById("support-dialog");
  document.querySelectorAll("[data-open-support]").forEach((button) => button.addEventListener("click", () => supportDialog?.showModal?.()));
  document.querySelectorAll("[data-close-support]").forEach((button) => button.addEventListener("click", () => supportDialog?.close?.()));
  supportDialog?.addEventListener("click", (event) => { if (event.target === supportDialog) supportDialog.close(); });
  document.querySelectorAll(".reveal-value").forEach((button) => {
    button.addEventListener("click", () => {
      const value = button.parentElement?.querySelector(".private-value");
      if (!value) return;
      const visible = button.dataset.shown === "1";
      value.textContent = visible ? "••••••••" : value.dataset.privateValue;
      button.dataset.shown = visible ? "0" : "1";
      button.textContent = visible ? "Show" : "Hide";
    });
  });

  const phoneInput = document.querySelector("#pay-form input[name='phone']");
  const networkHint = document.getElementById("network-hint");
  let detectTimer = null;

  if (phoneInput && networkHint) {
    phoneInput.addEventListener("input", () => {
      clearTimeout(detectTimer);
      detectTimer = setTimeout(async () => {
        const phone = phoneInput.value.trim();
        if (phone.length < 9) return;
        try {
          const res = await fetch(`/api/network/detect?phone=${encodeURIComponent(phone)}`);
          if (!res.ok) return;
          const data = await res.json();
          if (data.ok) {
            networkHint.textContent = `${data.network} detected · PIN opens on your phone`;
          }
        } catch (_) {
          /* ignore */
        }
      }, 350);
    });
  }

  const waiting = document.querySelector(".waiting-panel[data-order-id]");
  if (waiting) {
    const statusUrl = waiting.dataset.statusUrl;
    const successUrl = waiting.dataset.successUrl;
    const statusEl = document.getElementById("pay-status");
    let tries = 0;

    const poll = async () => {
      tries += 1;
      try {
        const res = await fetch(statusUrl, { headers: { Accept: "application/json" } });
        if (res.ok) {
          const data = await res.json();
          if (statusEl) statusEl.textContent = data.status;
          if (data.status === "success") {
            window.location.href = successUrl;
            return;
          }
          if (data.status === "failed" || data.status === "cancelled") {
            statusEl.textContent = data.status;
            return;
          }
        }
      } catch (_) {
        /* keep polling */
      }
      if (tries < 90) {
        setTimeout(poll, 3000);
      }
    };

    setTimeout(poll, 2500);
  }
})();