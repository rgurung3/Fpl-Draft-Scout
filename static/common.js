// Shared by the pages. Text from the server marks player names with ** **, with their points in
// brackets after, like "**Saka** (14) is carrying Team A". rich() escapes the text and then turns each
// marked name into bold, so a player's name stands out from team names and the words around it
// (a lot of names are two words or have a hyphen). Anything else in the text stays plain.
function rich(text) {
  const safe = String(text ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  return safe.replace(/\*\*([^*]+)\*\*/g, '<strong class="pl">$1</strong>');
}
