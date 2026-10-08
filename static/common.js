// Shared by the pages. Team (manager) names are often several words, or even have a footballer's name in
// them, so inside a sentence they blur into the words around them. The server wraps them in ** ** and
// rich() escapes the text and then turns each marked name into bold. mgr() does the same for one name the
// page already has in its hands. Anything else in the text stays plain.
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const escapeText = text => String(text ?? "").replace(/[&<>"]/g, c => ESCAPES[c]);

function rich(text) {
  return escapeText(text).replace(/\*\*([^*]+)\*\*/g, '<strong class="mgr">$1</strong>');
}

function mgr(name) {
  return `<strong class="mgr">${escapeText(name)}</strong>`;
}
