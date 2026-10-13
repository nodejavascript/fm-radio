/* The two small behaviours this page needs, and nothing else. */
(function () {
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("[data-copy]") : null;
    if (!btn) return;
    var block = btn.closest(".code");
    var code = block && block.querySelector("pre code");
    if (!code) return;
    var text = code.textContent;
    var done = function () {
      var was = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () {
        btn.textContent = was;
      }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        done();
      } catch (e) {
        /* the reader can still select it by hand */
      }
      ta.remove();
    }
  });
})();
