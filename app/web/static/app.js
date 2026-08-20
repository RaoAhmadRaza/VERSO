/* VERSO — upload, then bidirectional source↔parse linking (PLAN.md §8).
   Nothing in the UI matters as much as the linking working smoothly: it is how you
   debug a parse without reading JSON. */

(function () {
  "use strict";

  // ------------------------------------------------------------------ upload

  // Wall clock is dominated by output tokens, and output scales with how much is *in* the
  // resume, not with how long the file takes to read. Two measured points:
  //   26 KB / 37 lines / 2,535 tokens of JSON  ->  ~60s
  //   69 KB / 150 lines / 9,252 tokens of JSON -> ~215s
  // File size is a rough proxy for that, and the only signal available before the parse
  // runs. Fitted to those points, deliberately erring high — an estimate that overruns
  // reads as broken, one that comes in early reads as fast.
  const ESTIMATE_FLOOR_MS = 30000;
  const ESTIMATE_PER_KB_MS = 2600;
  const ESTIMATE_CAP_MS = 420000;

  function estimateFor(file) {
    const kb = file.size / 1024;
    return Math.min(ESTIMATE_CAP_MS, ESTIMATE_FLOOR_MS + kb * ESTIMATE_PER_KB_MS);
  }

  // True statements about what is happening to the file, so the wait explains the product
  // rather than filling time. Anything here that stops being true is a bug.
  const FACTS = [
    "Your file never reaches the model. It only ever sees numbered lines of text.",
    "Bytes to text is deterministic. The same file produces the same lines every time.",
    "Every field you get back carries the line numbers it came from.",
    "Two-column layouts are found by clustering block positions, not by guessing.",
    "Quoted text is checked back against the exact lines it claims to come from.",
    "Any line that cannot be classified is shown to you, never dropped.",
    "Repeated page footers are removed — and listed, so the removal is auditable.",
    "Skill categories come from a fixed list, so the same skill always lands in one place.",
    "Letter-spaced headings are rebuilt from the spacing between glyphs.",
    "The coverage score counts every non-blank line in your document.",
    "Dates keep the wording you wrote. Nothing is reformatted behind your back.",
    "White-on-white keyword stuffing is extracted and flagged, not silently kept.",
    "Time here tracks how much is in your resume, not how big the file is.",
    "A detailed skills section is the slowest part — every entry is classified separately.",
  ];

  function formatClock(ms) {
    const total = Math.max(0, Math.round(ms / 1000));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  const dropzone = document.getElementById("dropzone");
  if (dropzone) {
    const input = document.getElementById("file");
    const processing = document.getElementById("processing");
    const failure = document.getElementById("failure");
    const failureMessage = document.getElementById("failure-message");
    const stages = [...document.querySelectorAll(".stages li")];
    const fill = document.getElementById("fill");
    const elapsedEl = document.getElementById("elapsed");
    const remainingEl = document.getElementById("remaining");
    const factEl = document.getElementById("fact");
    const nameEl = document.getElementById("parsing-name");
    const sizeEl = document.getElementById("parsing-size");

    const show = (el) => el && (el.hidden = false);
    const hide = (el) => el && (el.hidden = true);

    // Labelled stages reflect the real shape of the work: extraction is local and finishes
    // in well under a second, structuring is one model call and owns effectively all of
    // the wait, verification is local again. So extraction is marked done quickly and the
    // display then holds on structuring until the response actually lands — marching the
    // bar onward on a timer would claim progress the client cannot observe.
    function startProgress(file) {
      const began = Date.now();
      const estimate = estimateFor(file);
      let factIndex = Math.floor(Math.random() * FACTS.length);

      nameEl.textContent = file.name;
      sizeEl.textContent = `${(file.size / 1024).toFixed(0)} KB`;
      stages.forEach((s) => s.classList.remove("active", "done"));
      stages[0].classList.add("active");
      fill.classList.remove("overrun");

      function nextFact() {
        factEl.classList.remove("shown");
        setTimeout(() => {
          factEl.textContent = FACTS[factIndex++ % FACTS.length];
          factEl.classList.add("shown");
        }, 350);
      }
      nextFact();

      const toStructuring = setTimeout(() => {
        stages[0].classList.replace("active", "done");
        stages[1].classList.add("active");
      }, 700);

      const facts = setInterval(nextFact, 6000);
      const tick = setInterval(() => {
        const elapsed = Date.now() - began;
        elapsedEl.textContent = formatClock(elapsed);

        // Ease toward 92% over the baseline and hold. The bar never completes on a guess:
        // only a real response fills it.
        const ratio = elapsed / estimate;
        fill.style.width = `${Math.min(92, 92 * (1 - Math.exp(-2.2 * ratio)))}%`;

        if (elapsed < estimate) {
          remainingEl.textContent = `about ${formatClock(estimate - elapsed)} left`;
        } else {
          // Past the estimate, a countdown reading zero would be a lie. Say what is true, and
          // say it so it reads as expected rather than broken — a long resume genuinely takes
          // minutes, and the previous wording made correct behaviour look like a hang.
          remainingEl.textContent = "past the estimate — a detailed resume takes several minutes";
          fill.classList.add("overrun");
        }
      }, 250);

      return function stop(completed) {
        clearTimeout(toStructuring);
        clearInterval(facts);
        clearInterval(tick);
        if (completed) {
          stages[1].classList.replace("active", "done");
          stages[2].classList.add("active");
          fill.classList.remove("overrun");
          fill.style.width = "100%";
        }
      };
    }

    async function upload(file) {
      if (!file) return;
      hide(dropzone);
      hide(failure);
      show(processing);
      const stop = startProgress(file);

      const body = new FormData();
      body.append("file", file);
      try {
        const response = await fetch("/api/parse", { method: "POST", body });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.error || `The server returned ${response.status}.`);
        }
        stop(true);
        window.location = "/?sha=" + encodeURIComponent(payload.sha256);
      } catch (error) {
        stop(false);
        hide(processing);
        // A dropped connection reads as a TypeError from fetch, which tells the user
        // nothing. The usual cause is the server restarting mid-request.
        failureMessage.textContent =
          error instanceof TypeError
            ? "Lost the connection to the server before it answered. If it was started with --reload, editing a file restarts it and cancels the upload."
            : error.message;
        show(failure);
      }
    }

    input.addEventListener("change", () => upload(input.files[0]));
    document.getElementById("retry").addEventListener("click", () => {
      hide(failure);
      show(dropzone);
      input.value = "";
    });

    ["dragenter", "dragover"].forEach((name) =>
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("over");
      })
    );
    ["dragleave", "drop"].forEach((name) =>
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.remove("over");
      })
    );
    dropzone.addEventListener("drop", (event) => upload(event.dataTransfer.files[0]));
    dropzone.addEventListener("submit", (event) => event.preventDefault());
  }

  // ------------------------------------------------------- bidirectional link

  const source = document.getElementById("source");
  if (!source) return;

  const srcLines = new Map(); // line number -> <li>
  const bars = new Map();     // line number -> gutter bar
  const claimants = new Map(); // line number -> [elements on the right citing it]

  document.querySelectorAll(".src-line").forEach((el) =>
    srcLines.set(Number(el.dataset.line), el)
  );
  document.querySelectorAll(".bar").forEach((el) =>
    bars.set(Number(el.dataset.line), el)
  );

  // Built once, so hovering never costs a query.
  document.querySelectorAll("[data-lines]").forEach((el) => {
    const numbers = el.dataset.lines
      .split(",")
      .map((n) => Number(n.trim()))
      .filter(Boolean);
    el._lines = numbers;
    numbers.forEach((n) => {
      if (!claimants.has(n)) claimants.set(n, []);
      claimants.get(n).push(el);
    });
  });

  function paint(numbers, on) {
    numbers.forEach((n) => {
      srcLines.get(n)?.classList.toggle("lit", on);
      bars.get(n)?.classList.toggle("pulse", on);
    });
  }

  // right -> left
  document.querySelectorAll("[data-lines]").forEach((el) => {
    el.addEventListener("mouseenter", () => {
      paint(el._lines, true);
      const first = srcLines.get(el._lines[0]);
      if (first) first.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    el.addEventListener("mouseleave", () => paint(el._lines, false));
  });

  // left -> right
  srcLines.forEach((el, number) => {
    el.addEventListener("mouseenter", () => {
      paint([number], true);
      (claimants.get(number) || []).forEach((c) => c.classList.add("lit"));
    });
    el.addEventListener("mouseleave", () => {
      paint([number], false);
      (claimants.get(number) || []).forEach((c) => c.classList.remove("lit"));
    });
  });

  // gutter -> scroll to line
  bars.forEach((bar, number) => {
    bar.addEventListener("click", () =>
      srcLines.get(number)?.scrollIntoView({ block: "center", behavior: "smooth" })
    );
  });
})();

/* ---------------------------------------------------------------- profile tabs */
/* Panels are all rendered server-side; switching is show/hide, so there is no
   second fetch and no state to lose. */
(function tabs() {
  const bar = document.querySelector('.tabs');
  if (!bar) return;
  const panels = [...document.querySelectorAll('.panel')];
  bar.addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    const wanted = tab.dataset.tab;
    [...bar.querySelectorAll('.tab')].forEach((t) => t.classList.toggle('active', t === tab));
    panels.forEach((p) => p.classList.toggle('active', p.dataset.panel === wanted));
  });
})();

/* ------------------------------------------------------------- profile editing */
/* Read view and inputs are both rendered; the toggle swaps which is visible, so an
   abandoned edit needs no revert -- reloading discards it. */
(function editing() {
  document.querySelectorAll('.edit-toggle').forEach((button) => {
    button.addEventListener('click', () => {
      const panel = button.closest('.panel');
      const on = panel.classList.toggle('editing');
      button.textContent = on ? 'Cancel' : 'Edit';
      if (!on) panel.querySelector('form').reset();
    });
  });
})();
