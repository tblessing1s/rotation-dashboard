import { describe, expect, it } from "vitest";
import { _editsForSave } from "./HistoryTab.jsx";

// CONFIRMED LIVE: every row was previously resubmitted on every save (price/
// stock_price/extrinsic are always pre-filled, never blank), and the backend
// applies whatever a present field says with no comparison against the
// current value — so an untouched close_short row's on-screen extrinsic (0,
// possibly stale) got re-saved as a fresh "correction" every time, able to
// silently zero out a correctly-derived extrinsic_sold. _editsForSave must
// make an untouched row a no-op.

function row(overrides) {
  return {
    id: "e1", strike: "43", contracts: "1", expiration: "2026-09-18",
    date: "2026-09-24", price: "2.54", stock_price: "45.525", extrinsic: "0",
    editableExtrinsic: true, isOpen: false, isShare: false,
    ...overrides,
  };
}

describe("_editsForSave", () => {
  it("submits nothing for a row identical to its loaded snapshot", () => {
    const r = row();
    expect(_editsForSave([r], { e1: row() })).toEqual([]);
  });

  it("submits only the edited field(s), not every field, for a touched row", () => {
    const original = row();
    const edited = row({ extrinsic: "0.67" });
    const edits = _editsForSave([edited], { e1: original });
    expect(edits).toEqual([{ id: "e1", entry_extrinsic: 0.67 }]);
  });

  it("uses entry_extrinsic for a close row, extrinsic for an open row", () => {
    const openOriginal = row({ id: "o1", editableExtrinsic: false, isOpen: true, extrinsic: "0.5" });
    const openEdited = row({ id: "o1", editableExtrinsic: false, isOpen: true, extrinsic: "0.6" });
    const edits = _editsForSave([openEdited], { o1: openOriginal });
    expect(edits).toEqual([{ id: "o1", extrinsic: 0.6 }]);
  });

  it("an untouched row never reaches the server even when its field is 0", () => {
    // The exact failure mode: a close row correctly showing extrinsic 0.14
    // (from a real roll adoption) but never opened by the operator this
    // save — must not be resubmitted just because it's sitting in `rows`.
    const original = row({ id: "roll", extrinsic: "0.14" });
    const untouched = row({ id: "roll", extrinsic: "0.14" });
    const touched = row({ id: "e1", extrinsic: "0.67" });
    const edits = _editsForSave([untouched, touched], { roll: original, e1: row() });
    expect(edits).toEqual([{ id: "e1", entry_extrinsic: 0.67 }]);
  });

  it("submits multiple changed fields on one row together", () => {
    const original = row();
    const edited = row({ date: "2026-09-08", price: "2.02" });
    const edits = _editsForSave([edited], { e1: original });
    expect(edits).toEqual([{ id: "e1", date: "2026-09-08", price: 2.02 }]);
  });

  it("clearing a field to blank sends null, not a skip", () => {
    const original = row();
    const edited = row({ expiration: "" });
    const edits = _editsForSave([edited], { e1: original });
    expect(edits).toEqual([{ id: "e1", expiration: null }]);
  });

  it("skips rows with no loaded snapshot changes across a full unedited table", () => {
    const rows = [row({ id: "a" }), row({ id: "b" }), row({ id: "c" })];
    const original = { a: row({ id: "a" }), b: row({ id: "b" }), c: row({ id: "c" }) };
    expect(_editsForSave(rows, original)).toEqual([]);
  });
});
