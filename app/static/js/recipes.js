// Recipe list filters: the tag search box narrows the tag menu as you type,
// and the minutes field submits itself since there's no visible Apply button
// once you're outside the search box.
(() => {
  const wrap = document.querySelector("[data-tag-search]");
  if (wrap) {
    const input = wrap.querySelector(".tag-search__input");
    const menu = wrap.querySelector(".tag-search__menu");
    const options = Array.from(menu.querySelectorAll(".tag-search__option"));
    const noMatch = menu.querySelector(".tag-search__empty--no-match");

    const openMenu = () => { menu.hidden = false; };
    const closeMenu = () => { menu.hidden = true; };

    const filterOptions = () => {
      const term = input.value.trim().toLowerCase();
      let visible = 0;
      options.forEach((option) => {
        const match = !term || option.dataset.tagName.includes(term);
        option.hidden = !match;
        if (match) visible += 1;
      });
      if (noMatch) noMatch.hidden = !(term && visible === 0 && options.length > 0);
    };

    input.addEventListener("focus", openMenu);
    input.addEventListener("input", () => {
      openMenu();
      filterOptions();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeMenu();
        input.blur();
      }
    });

    document.addEventListener("click", (event) => {
      if (!wrap.contains(event.target)) closeMenu();
    });
  }

  const minutesInput = document.querySelector("[data-auto-submit]");
  if (minutesInput) {
    minutesInput.addEventListener("change", () => minutesInput.form.submit());
  }
})();
