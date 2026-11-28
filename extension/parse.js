// Turns Luma scanner output into results for Reachy. Shared by the extension and node tests.
(function (root) {
  const EXPRESS = /^https:\/\/api\.luma\.com\/event\/admin\/check-in\/express(\?|$)/;
  const TICKET = /^https:\/\/(www\.)?(luma\.com|lu\.ma)\/check-in\//;
  const LUMA_LINK = /^https:\/\/(www\.)?(luma\.com|lu\.ma)\//;

  // Luma's Express check-in API response -> {kind, name} | null (null = not a scan result).
  function parseScanResponse(url, status, body) {
    if (!EXPRESS.test(url || "")) return null;
    let data = null;
    try { data = JSON.parse(body); } catch (_) {}
    const name = (data && data.guest && typeof data.guest.name === "string" && data.guest.name.trim()) || null;
    if (status === 200 && data && data.status === "success") return { kind: "success", name };
    if (status === 200 && data && data.status === "already-checked-in") return { kind: "already", name };
    return { kind: "failure", name };
  }

  // A QR code Chrome saw in the camera -> "wrong_code" if it's a Luma link that isn't a ticket.
  function classifySeenCode(text) {
    if (LUMA_LINK.test(text || "") && !TICKET.test(text)) return "wrong_code";
    return null;
  }

  // Express mode only matters on the camera scanner view (not the List tab or other pages).
  function isScannerPage(pathname, search) {
    return /\/check-in\/[^/]+\/scan\/?$/.test(pathname || "") && !/[?&]tab=/.test(search || "");
  }

  // What kind of QR it was, without its contents (so a ticket token never lands in the log).
  function describeCode(text) {
    text = text || "";
    try {
      const u = new URL(text);
      return `${u.protocol}//${u.host}${u.pathname.split("/").slice(0, 2).join("/")}… (${text.length} chars)`;
    } catch (_) {
      return `plain text (${text.length} chars)`;
    }
  }

  root.ReachyParse = { parseScanResponse, classifySeenCode, isScannerPage, describeCode };
  if (typeof module !== "undefined") module.exports = root.ReachyParse;
})(typeof globalThis !== "undefined" ? globalThis : this);
