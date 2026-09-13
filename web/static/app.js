/* InstaCook — the small amount of behaviour a recipe page actually needs.
   Progress is per-device and disposable, so localStorage is the right home for
   it: it should survive walking away from the hob, and nothing more. */

(function () {
  "use strict";

  var store = {
    get: function (key) {
      try { return localStorage.getItem(key) === "1"; } catch (e) { return false; }
    },
    set: function (key, on) {
      try { on ? localStorage.setItem(key, "1") : localStorage.removeItem(key); }
      catch (e) { /* private window, blocked storage — carry on regardless */ }
    }
  };

  function countFor(prefix) {
    var boxes = document.querySelectorAll('[data-checklist="' + prefix + '"] input');
    var steps = document.querySelectorAll('[data-steps="' + prefix + '"] li');
    var total = boxes.length || steps.length;
    var done = 0;
    boxes.forEach(function (b) { if (b.checked) done++; });
    steps.forEach(function (s) { if (s.dataset.done === "1") done++; });
    return { done: done, total: total };
  }

  function paintProgress(prefix) {
    var el = document.querySelector('[data-progress="' + prefix + '"]');
    if (!el) return;
    var c = countFor(prefix);
    el.textContent = c.total ? c.done + " of " + c.total : "";
  }

  // --- ingredients ---------------------------------------------------------
  document.querySelectorAll(".check input[data-key]").forEach(function (box) {
    box.checked = store.get(box.dataset.key);
    box.addEventListener("change", function () {
      store.set(box.dataset.key, box.checked);
      paintProgress(box.dataset.key.split(":").slice(0, 2).join(":"));
    });
  });

  // --- steps: tap anywhere on the row --------------------------------------
  document.querySelectorAll(".steps li[data-key]").forEach(function (li) {
    if (store.get(li.dataset.key)) li.dataset.done = "1";
    li.setAttribute("role", "button");
    li.setAttribute("tabindex", "0");
    function toggle() {
      var on = li.dataset.done !== "1";
      li.dataset.done = on ? "1" : "0";
      li.setAttribute("aria-pressed", on ? "true" : "false");
      store.set(li.dataset.key, on);
      paintProgress(li.dataset.key.split(":").slice(0, 2).join(":"));
    }
    li.addEventListener("click", toggle);
    li.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });

  document.querySelectorAll("[data-progress]").forEach(function (el) {
    paintProgress(el.dataset.progress);
  });

  // --- keep the screen on while cooking ------------------------------------
  // A phone locking mid-step with oily hands is the single most annoying thing
  // about cooking from a screen. Best-effort: unsupported browsers just skip it.
  var lock = null;
  function holdScreen() {
    if (!("wakeLock" in navigator)) return;
    navigator.wakeLock.request("screen").then(function (l) {
      lock = l;
      l.addEventListener("release", function () { lock = null; });
    }).catch(function () { /* denied or battery saver — not worth surfacing */ });
  }
  if (document.querySelector(".steps")) {
    holdScreen();
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && lock === null) holdScreen();
    });
  }

  // --- guided cooking ------------------------------------------------------
  // Not wired yet. The context the agent needs already ships with the page, so
  // switching this on is: read the JSON, hand it to the ElevenLabs agent as
  // session context, and drop the disabled attribute.
  var ctx = document.getElementById("cook-context");
  window.InstaCook = {
    context: ctx ? JSON.parse(ctx.textContent) : null,
    startVoice: function () {
      throw new Error("Voice guidance is not connected yet.");
    }
  };
})();
