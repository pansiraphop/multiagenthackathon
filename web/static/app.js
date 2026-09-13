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

  // --- guided cooking: one play/pause button for spoken instructions -------
  var ctxEl = document.getElementById("cook-context");
  var context = ctxEl ? JSON.parse(ctxEl.textContent) : null;

  var guide = {
    segments: null,
    index: 0,
    loading: false,
    awaitingNext: false,
    panel: document.getElementById("guide"),
    count: document.getElementById("guide-count"),
    label: document.getElementById("guide-label"),
    text: document.getElementById("guide-text"),
    status: document.getElementById("guide-status"),
    audio: document.getElementById("guide-audio"),
    toggle: document.getElementById("cook-toggle"),
    toggleLabel: document.getElementById("cook-btn-label")
  };

  function setStatus(msg) {
    if (guide.status) guide.status.textContent = msg || "";
  }

  function setButton(state, label) {
    if (!guide.toggle) return;
    guide.toggle.dataset.state = state;
    guide.toggle.setAttribute("aria-pressed", state === "playing" ? "true" : "false");
    if (guide.toggleLabel) guide.toggleLabel.textContent = label;
  }

  function setBusy(on) {
    guide.loading = on;
    if (guide.toggle) guide.toggle.disabled = on;
  }

  function highlightStep(stepIndex) {
    document.querySelectorAll(".steps li[data-current]").forEach(function (li) {
      delete li.dataset.current;
    });
    if (stepIndex === null || stepIndex === undefined) return;
    var li = document.querySelector('.steps li[data-step-index="' + stepIndex + '"]');
    if (!li) return;
    li.dataset.current = "1";
    li.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function markStepDone(stepIndex) {
    if (stepIndex === null || stepIndex === undefined) return;
    var li = document.querySelector('.steps li[data-step-index="' + stepIndex + '"]');
    if (!li || !li.dataset.key) return;
    li.dataset.done = "1";
    li.setAttribute("aria-pressed", "true");
    store.set(li.dataset.key, true);
    paintProgress(li.dataset.key.split(":").slice(0, 2).join(":"));
  }

  function paintSegment() {
    var seg = guide.segments[guide.index];
    var total = guide.segments.length;
    if (guide.count) guide.count.textContent = "Beat " + (guide.index + 1) + " of " + total;
    if (guide.label) guide.label.textContent = (seg.label || "").replace(/-/g, " ");
    if (guide.text) guide.text.textContent = seg.text;
    highlightStep(seg.step_index);
  }

  function prefetch(index) {
    if (!guide.segments || index >= guide.segments.length) return;
    fetch(guide.segments[index].audio_url).catch(function () {});
  }

  function pauseAfterSection() {
    var seg = guide.segments[guide.index];
    if (seg && seg.step_index !== null && seg.step_index !== undefined) {
      markStepDone(seg.step_index);
    }
    if (guide.index >= guide.segments.length - 1) {
      setStatus("You're done — enjoy.");
      highlightStep(null);
      setButton("done", "Done");
      guide.awaitingNext = false;
      return;
    }
    guide.index += 1;
    guide.awaitingNext = true;
    paintSegment();
    prefetch(guide.index);
    setButton("paused", "Resume");
    setStatus("Paused — tap when ready");
  }

  function playCurrent() {
    var seg = guide.segments[guide.index];
    guide.awaitingNext = false;
    setBusy(true);
    setStatus("Loading voice…");
    paintSegment();
    setButton("playing", "Pause");

    guide.audio.pause();
    guide.audio.src = seg.audio_url + "?t=" + Date.now();
    guide.audio.load();

    var playPromise = guide.audio.play();
    if (playPromise && playPromise.then) {
      playPromise.then(function () {
        setStatus("Playing");
        setBusy(false);
        prefetch(guide.index + 1);
      }).catch(function () {
        setStatus("Tap again if audio was blocked");
        setButton("paused", "Resume");
        setBusy(false);
      });
    } else {
      setBusy(false);
    }

    guide.audio.onended = function () {
      setStatus("");
      pauseAfterSection();
    };
    guide.audio.onerror = function () {
      setStatus("Couldn't load speech — is ElevenLabs configured?");
      setButton("paused", "Resume");
      setBusy(false);
    };
  }

  function pauseGuide() {
    if (!guide.audio) return;
    guide.audio.pause();
    setButton("paused", "Resume");
    setStatus("Paused");
  }

  function resumeGuide() {
    if (!guide.segments) return;
    // After a section ends we advance and wait — Resume plays the next beat.
    if (guide.awaitingNext || (guide.audio && guide.audio.ended)) {
      playCurrent();
      return;
    }
    var playPromise = guide.audio.play();
    setButton("playing", "Pause");
    if (playPromise && playPromise.then) {
      playPromise.then(function () {
        setStatus("Playing");
      }).catch(function () {
        setStatus("Tap again if audio was blocked");
        setButton("paused", "Resume");
      });
    } else {
      setStatus("Playing");
    }
  }

  function startGuide() {
    if (!context || !context.guidance_url) {
      setStatus("No guidance for this meal");
      return;
    }
    setBusy(true);
    setStatus("Writing detailed steps…");
    setButton("loading", "Preparing…");

    fetch(context.guidance_url + "?force=0")
      .then(function (res) {
        if (!res.ok) throw new Error("guidance " + res.status);
        return res.json();
      })
      .then(function (data) {
        guide.segments = data.segments || [];
        if (!guide.segments.length) throw new Error("empty guidance");
        guide.index = 0;
        if (guide.panel) guide.panel.hidden = false;
        playCurrent();
      })
      .catch(function () {
        setStatus("Couldn't load guidance");
        setButton("idle", "Begin Cooking");
        setBusy(false);
      });
  }

  function onToggle() {
    if (guide.loading) return;
    if (!guide.segments) {
      startGuide();
      return;
    }
    if (guide.toggle && guide.toggle.dataset.state === "done") return;
    if (guide.audio && !guide.audio.paused && !guide.audio.ended) {
      pauseGuide();
    } else {
      resumeGuide();
    }
  }

  if (guide.toggle) guide.toggle.addEventListener("click", onToggle);

  window.InstaCook = {
    context: context,
    startVoice: startGuide,
    toggleVoice: onToggle
  };
})();
