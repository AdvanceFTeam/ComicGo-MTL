const reader = document.querySelector("[data-reader]");

if (reader) {
  window.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      window.scrollBy({ top: window.innerHeight * 0.8, behavior: "smooth" });
    }
    if (event.key === "ArrowUp") {
      window.scrollBy({ top: -window.innerHeight * 0.8, behavior: "smooth" });
    }
  });
}
