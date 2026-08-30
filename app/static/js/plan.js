/* Progressive enhancement for the week board.
 *
 * Every form here already works as a plain POST-and-redirect -- that's what
 * happens if this file fails to load, or JS is off. When it *is* loaded, it
 * intercepts those same forms, does the POST as a fetch instead, and swaps
 * in the HTML the server sends back. The point is purely to avoid the full
 * page reload: on mobile that reload scrolls you back to the top of the
 * week, which is exactly where you don't want to be after rerolling a
 * Thursday you found by scrolling down to it.
 */
(function () {
  "use strict";

  function setFlashes(html) {
    var el = document.getElementById("flashes");
    if (el && html !== undefined) el.innerHTML = html;
  }

  function setActions(html) {
    var el = document.getElementById("week-actions");
    if (el && html !== undefined) el.outerHTML = html;
  }

  // A day-scoped action (reroll, lock, skip, save note...) re-renders just
  // that one card plus the actions bar -- clearing the last empty day, say,
  // can make "Fill" disappear, so the bar has to stay in sync too.
  function applyDayUpdate(payload, collapseDetails) {
    var card = document.querySelector('[data-day="' + payload.day_iso + '"]');
    var reopen = !collapseDetails && !!(card && card.querySelector(".day__details[open]"));
    if (card) card.outerHTML = payload.day_html;
    if (reopen) {
      var details = document.querySelector(
        '[data-day="' + payload.day_iso + '"] .day__details'
      );
      if (details) details.open = true;
    }
    setActions(payload.actions_html);
    setFlashes(payload.flash_html);
  }

  // A week-scoped action (fill empty days, reroll unlocked) can touch every
  // card at once, so the whole board comes back.
  function applyWeekUpdate(payload) {
    var board = document.getElementById("week-board");
    if (board) board.outerHTML = payload.board_html;
    setActions(payload.actions_html);
    setFlashes(payload.flash_html);
  }

  function setBusy(form, busy) {
    var button = form.querySelector("button[type=submit]");
    if (button) button.disabled = busy;
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-board-form")) {
      return;
    }

    event.preventDefault();
    setBusy(form, true);

    // Saving the servings/note form is the one action meant to close its
    // own "Servings & note" disclosure rather than leave it open.
    var collapseDetails = !!form.closest(".day__details");

    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) throw new Error("board update failed");
        return response.json();
      })
      .then(function (payload) {
        if (payload.board_html !== undefined) {
          applyWeekUpdate(payload);
        } else {
          applyDayUpdate(payload, collapseDetails);
        }
      })
      .catch(function () {
        // Network hiccup or server error: fall back to the plain submit
        // this would have been without JS, rather than leaving the button
        // dead with no feedback.
        form.submit();
      })
      .finally(function () {
        setBusy(form, false);
      });
  });
})();
