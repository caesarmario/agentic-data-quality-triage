/**
 * Contract tests for the source-owned shadcn/ui compatibility layer.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import assert from "node:assert/strict";
import test from "node:test";
import { Badge as ShadcnBadge, badgeVariants } from "../ui/badge";
import { Button, buttonVariants } from "../ui/button";
import { Card as ShadcnCard } from "../ui/card";
import { Badge, Card } from "../ui/primitives";

// Existing callers retain their semantic class API while rendering source-owned components.
test("legacy primitives delegate to shadcn Card and Badge components", () => {
  const card = Card({ children: "Control-plane evidence" });
  const badge = Badge({ children: "triaged", tone: "warning" });

  assert.equal(card.type, ShadcnCard);
  assert.match(String(card.props.className), /\bcard\b/);
  assert.equal(badge.type, ShadcnBadge);
  assert.equal(badge.props.variant, "secondary");
  assert.match(String(badge.props.className), /\bbadge-warning\b/);
});

test("source-owned Button and Badge expose official shadcn variants", () => {
  assert.match(buttonVariants({ variant: "destructive" }), /text-destructive/);
  assert.match(badgeVariants({ variant: "outline" }), /border-border/);
  assert.equal(Button({ children: "Run triage" }).props["data-slot"], "button");
});
