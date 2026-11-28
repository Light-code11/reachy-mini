import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { parseScanResponse, classifySeenCode } = require("./parse.js");
const fx = (f) => readFileSync(new URL(`./fixtures/${f}`, import.meta.url), "utf8");
const EXPRESS = "https://api.luma.com/event/admin/check-in/express";

test("express success gives the guest's full name", () => {
  assert.deepEqual(parseScanResponse(EXPRESS, 200, fx("express-success.json")), { kind: "success", name: "Test Guest" });
});

test("express not-found is a failure", () => {
  assert.deepEqual(parseScanResponse(EXPRESS, 200, fx("express-not-found.json")), { kind: "failure", name: null });
});

test("any other express status is a failure, keeping the guest name if present", () => {
  const body = JSON.stringify({ status: "pending-approval", guest: { name: "Sam Lee" } });
  assert.deepEqual(parseScanResponse(EXPRESS, 200, body), { kind: "failure", name: "Sam Lee" });
});

test("http error from express is a failure", () => {
  assert.deepEqual(parseScanResponse(EXPRESS, 500, "oops"), { kind: "failure", name: null });
});

test("success without a guest name still succeeds, nameless", () => {
  assert.deepEqual(parseScanResponse(EXPRESS, 200, '{"status":"success"}'), { kind: "success", name: null });
});

test("unrelated endpoints are ignored", () => {
  assert.equal(parseScanResponse("https://api.luma.com/event/admin/get-guests?x=1", 200, "{}"), null);
  assert.equal(parseScanResponse("https://api.luma.com/chat/messages/sync", 200, "{}"), null);
});

test("event-page link instead of a ticket is a wrong code", () => {
  assert.equal(classifySeenCode("https://luma.com/8s8mvgm0?tk=XhNacH"), "wrong_code");
  assert.equal(classifySeenCode("https://lu.ma/abc123"), "wrong_code");
});

test("ticket codes and non-Luma codes are not wrong codes", () => {
  assert.equal(classifySeenCode("https://luma.com/check-in/evt-ZLcgMdLTqHF8bK3?pk=g-abc"), null);
  assert.equal(classifySeenCode("https://example.com/menu"), null);
});

test("already-checked-in is its own kind, with the guest name", () => {
  const body = JSON.stringify({ status: "already-checked-in", message: "This guest is already checked in.", guest: { name: "Sam Lee" } });
  assert.deepEqual(parseScanResponse(EXPRESS, 200, body), { kind: "already", name: "Sam Lee" });
});

const { isScannerPage } = require("./parse.js");

test("only the scanner view (not list or guest pages) needs Express mode", () => {
  assert.equal(isScannerPage("/check-in/evt-ZLcgMdLTqHF8bK3/scan", ""), true);
  assert.equal(isScannerPage("/check-in/evt-ZLcgMdLTqHF8bK3/scan", "?guest=gst-1"), true);
  assert.equal(isScannerPage("/check-in/evt-ZLcgMdLTqHF8bK3/scan", "?tab=approved"), false);
  assert.equal(isScannerPage("/check-in/evt-ZLcgMdLTqHF8bK3", ""), false);
});


test("describeCode never logs a code's contents", () => {
  const d = ReachyParse.describeCode("https://wallet.example.com/pass/abc123secret?pk=g-xyz");
  assert.ok(d.startsWith("https://wallet.example.com/pass"), d);
  assert.ok(!d.includes("abc123secret") && !d.includes("g-xyz"), d);
  assert.equal(ReachyParse.describeCode("GUEST-12345"), "plain text (11 chars)");
});
