const stickyTop = document.querySelector(".sticky-top");
const foods = [...document.querySelectorAll("details.food")];
const autoCollapse = document.querySelector("#auto-collapse");
autoCollapse.checked = sessionStorage.getItem("mealietidy-auto-collapse") === "true";
const updateShadows = () => {
  const edge = getComputedStyle(stickyTop).position === "sticky" ? stickyTop.getBoundingClientRect().bottom : 0;
  for (const food of foods) {
    const title = food.querySelector("summary");
    title.classList.toggle("is-stuck", food.open && Math.abs(title.getBoundingClientRect().top - edge) < 1 && food.getBoundingClientRect().top < edge - 1);
  }
};
const updateStickyHeight = () => {
  document.documentElement.style.setProperty("--sticky-height", `${stickyTop.offsetHeight}px`);
  updateShadows();
};
new ResizeObserver(updateStickyHeight).observe(stickyTop);
window.addEventListener("scroll", updateShadows, {passive: true});
window.addEventListener("resize", updateShadows);
updateStickyHeight();
foods.forEach((food) => {
  food.addEventListener("toggle", updateShadows);
  const controls = [...food.querySelectorAll("[data-saved]")];
  const refresh = () => {
    const counts = {pending: 0, approve: Number(food.dataset.appliedCount), reject: 0};
    const labels = {pending: "Pending", approve: "Approved", reject: "Rejected"};
    food.querySelectorAll("input[type='radio'][data-saved]:checked").forEach((radio) => {
      counts[radio.value]++;
    });
    for (const [decision, count] of Object.entries(counts)) {
      food.querySelector(`[data-count="${decision}"]`).textContent = `${labels[decision]} ${count}`;
    }
    food.querySelector("[data-unsaved]").hidden = !controls.some((control) =>
      control.type === "radio"
        ? control.checked && control.value !== control.dataset.saved
        : control.value !== control.dataset.saved
    );
    if (autoCollapse.checked && counts.pending === 0) food.open = false;
  };
  food.addEventListener("input", refresh);
  food.addEventListener("change", refresh);
  refresh();
});
autoCollapse.addEventListener("change", () => {
  sessionStorage.setItem("mealietidy-auto-collapse", String(autoCollapse.checked));
  foods.forEach((food) => food.dispatchEvent(new Event("change")));
});
const form = document.querySelector("#review-form");
const status = document.querySelector("#save-status");
for (const conflictUsage of document.querySelectorAll(".conflict-usage")) {
  const ids = conflictUsage.dataset.foodIds.split(",");
  fetch(`${location.pathname}usage?food_ids=${encodeURIComponent(ids.join(","))}`)
    .then(async (response) => {
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Recipe counts unavailable");
      for (const item of conflictUsage.querySelectorAll("[data-food-id]")) {
        const counts = result[item.dataset.foodId];
        item.textContent = !counts || typeof counts.recipes !== "number"
          ? `${item.dataset.foodName}: unavailable`
          : `${item.dataset.foodName}: ${counts.recipes} recipe(s), `
            + `${counts.shopping_items} shopping item(s), `
            + `${counts.substitution_links} substitution link(s)`;
      }
    })
    .catch((error) => {
      conflictUsage.textContent = `Recipe counts unavailable: ${error.message}`;
    });
}
let saving = false;
form.addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "save") {
    if (saving) event.preventDefault();
    return;
  }
  event.preventDefault();
  if (saving) return;
  saving = true;
  const button = event.submitter;
  const data = new FormData(form);
  data.append("action", "save");
  const submitted = new Map(data);
  button.disabled = true;
  status.hidden = false;
  status.className = "muted";
  status.textContent = "Saving...";
  try {
    const response = await fetch(location.href, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: new URLSearchParams(data),
    });
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    const message = page.querySelector("#save-status")?.textContent || `Save failed (HTTP ${response.status})`;
    if (!response.ok) {
      status.className = "error";
      status.innerHTML = page.querySelector("#save-status")?.innerHTML || message;
      return;
    }
    const hash = page.querySelector("input[name='review_hash']")?.value;
    if (!hash) throw new Error("Save response was incomplete; reload before editing further");
    form.querySelector("input[name='review_hash']").value = hash;
    for (const control of form.querySelectorAll("[data-saved]")) {
      const saved = submitted.get(control.name);
      if (saved !== undefined) control.dataset.saved = saved;
    }
    for (const food of document.querySelectorAll("details.food")) food.dispatchEvent(new Event("change"));
    form.querySelector("#review-summary").textContent = page.querySelector("#review-summary").textContent;
    status.className = "success";
    status.textContent = message;
  } catch (error) {
    status.className = "error";
    status.textContent = error.message || "Save failed; reload before retrying";
  } finally {
    button.disabled = false;
    saving = false;
  }
});
