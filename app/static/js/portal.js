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