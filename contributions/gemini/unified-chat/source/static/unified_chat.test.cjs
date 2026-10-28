const test = require("node:test");
const assert = require("node:assert/strict");
const projection = require("./app.js");

test("providerCatalog separates display names from provider IDs and connector IDs", () => {
  assert.deepEqual(projection.providerCatalog.codex, {
    provider: "codex",
    connector_id: "codex_dot",
    display_name: "ChatGPT",
    character_key: "codex",
  });
  assert.deepEqual(projection.providerCatalog.claude, {
    provider: "claude",
    connector_id: "claude",
    display_name: "Claude",
    character_key: "claude",
  });
  assert.deepEqual(projection.providerCatalog.antigravity, {
    provider: "antigravity",
    connector_id: "antigravity",
    display_name: "Gemini",
    character_key: "gemini",
  });
});

test("providerBoardSummary projects independent per-provider statuses, provenance, and review targets", () => {
  const runs = [
    {
      id: "run-codex-1",
      provider: "codex",
      connector_id: "codex_dot",
      status: "succeeded",
      mode: "synthetic",
      run_kind: "local_fixture",
      provenance: "synthetic_fixture",
      requested_model: null,
      reported_model: "synthetic-codex-fixture",
    },
    {
      id: "run-claude-1",
      provider: "claude",
      connector_id: "claude",
      status: "failed",
      mode: "synthetic",
      run_kind: "local_fixture",
      provenance: "synthetic_fixture",
      error: "Synthetic fixture nonzero exit",
    },
    {
      id: "run-agy-review-1",
      provider: "antigravity",
      connector_id: "antigravity",
      status: "running",
      mode: "synthetic",
      run_kind: "local_fixture",
      provenance: "synthetic_fixture",
      review_of_run_id: "run-codex-1",
    },
  ];
  const board = projection.providerBoardSummary(runs);
  assert.equal(board.length, 3);
  assert.equal(board[0].display_name, "ChatGPT");
  assert.equal(board[0].provider, "codex");
  assert.equal(board[0].connector_id, "codex_dot");
  assert.equal(board[0].status, "succeeded");
  assert.equal(board[0].mode, "synthetic");
  assert.equal(board[0].requested_model, null);
  assert.equal(board[0].reported_model, "synthetic-codex-fixture");

  assert.equal(board[1].display_name, "Claude");
  assert.equal(board[1].status, "failed");
  assert.equal(board[1].error, "Synthetic fixture nonzero exit");

  assert.equal(board[2].display_name, "Gemini");
  assert.equal(board[2].provider, "antigravity");
  assert.equal(board[2].status, "running");
  assert.equal(board[2].review_of_run_id, "run-codex-1");
});

test("historical responses and current synthetic responses remain distinct in mode and usage projection", () => {
  const historicalRun = {
    id: "hist-1",
    provider: "antigravity",
    mode: "historical",
    run_kind: "historical_import",
    provenance: "historical_external_execution",
    status: "succeeded",
    receipt: { usage: { total_tokens: 1233 } },
  };
  const syntheticRun = {
    id: "synth-1",
    provider: "antigravity",
    mode: "synthetic",
    run_kind: "local_fixture",
    provenance: "synthetic_fixture",
    status: "succeeded",
    receipt: { usage: { total_tokens: 18 } },
  };
  assert.equal(projection.mode(historicalRun), "historical");
  assert.equal(projection.reportedUsage(historicalRun), 1233);
  assert.equal(projection.mode(syntheticRun), "synthetic");
  assert.equal(projection.reportedUsage(syntheticRun), null);
});
