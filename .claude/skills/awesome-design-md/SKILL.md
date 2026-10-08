---
name: awesome-design-md
description: Library of 74 DESIGN.md files describing the design systems of well-known brands (Stripe, Linear, Apple, Vercel, Notion, Airbnb...), each with colors, type scale, spacing, components and do/don't rules. Use when starting or restyling a UI and you want a real, proven design system as the reference instead of generic defaults.
metadata:
  origin: community (VoltAgent/awesome-design-md, see SOURCE.md)
---

# Awesome DESIGN.md

Each file in `designs/<brand>/DESIGN.md` is a plain-text analysis of one brand's
design system: YAML tokens at the top (colors, typography, radii, spacing), then
prose on layout, components, motion and do/don't rules. Coding agents can follow
one closely to produce UI that feels like that family of product.

## When to use

- A new page, app or dashboard needs a design direction.
- A UI looks generic and needs a real system to anchor it.
- The user names a brand look ("make it feel like Linear").

## How to use

1. List the options: `ls .claude/skills/awesome-design-md/designs/`.
2. Pick 1 to 3 candidates that fit the product's audience and mood. Read only the
   chosen files (each is 20-40 KB; do not load them all).
3. Copy the chosen file to the project as `DESIGN.md` (or a section of it), then
   adapt it: replace the brand's name, logo and proprietary fonts with the
   product's own or free equivalents (Google Fonts or system fonts).
4. Build the UI from its tokens and rules. Pair with `design-taste-frontend` for
   direction and `web-design-guidelines` to audit the result.

## Rules

- These are inspired analyses, not official brand kits. Use them for structure,
  rhythm and palette logic; never copy logos, trademarks or brand names into a
  product, and never present JokR's product as affiliated with the brand.
- Prefer mixing principles over cloning one brand pixel for pixel.

## Index

airbnb airtable apple binance bmw bmw-m bugatti cal claude clay clickhouse cohere 
coinbase composio cursor dell-1996 elevenlabs expo ferrari figma framer hashicorp hp ibm 
intercom kraken lamborghini linear.app lovable mastercard meta minimax mintlify miro 
mistral.ai mongodb nike nintendo-2001 notion nvidia ollama opencode.ai pinterest 
playstation posthog raycast renault replicate resend revolut runwayml sanity sentry 
shopify slack spacex spotify starbucks stripe supabase superhuman tesla theverge 
together.ai uber vercel vodafone voltagent warp webflow wired wise x.ai zapier 
