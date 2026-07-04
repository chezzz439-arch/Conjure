---
name: Conjure Kiosk
description: Voice-to-3D-print fabrication kiosk — machine-enamel dark UI where the conjured object is the hero.
colors:
  green-graphite: "#0f1713"
  graphite-deep: "#0b110e"
  surface: "#16211c"
  surface-raised: "#1c2a23"
  surface-lifted: "#17261f"
  border: "#2a3a32"
  border-soft: "#223029"
  paper: "#f2f5ef"
  paper-dim: "#b7c4ba"
  sage: "#8ba094"
  sage-faint: "#5c6f65"
  jade: "#45d38a"
  jade-lifted: "#55dd97"
  jade-dim: "#2fae6d"
  jade-edge: "#1e8a52"
  jade-ink: "#06170e"
  amber: "#e0a458"
  red: "#e5604f"
typography:
  display:
    fontFamily: "Archivo, -apple-system, system-ui, sans-serif"
    fontSize: "46px"
    fontWeight: 800
    lineHeight: 1.06
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Archivo, -apple-system, system-ui, sans-serif"
    fontSize: "34px"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Archivo, -apple-system, system-ui, sans-serif"
    fontSize: "27px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.015em"
  body:
    fontFamily: "Archivo, -apple-system, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: "IBM Plex Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.08em"
  mono-data:
    fontFamily: "IBM Plex Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  sm: "3px"
  md: "4px"
  lg: "6px"
spacing:
  touch-target: "48px"
  screen-edge: "clamp(20px, 4vw, 32px)"
  nav-height: "60px"
components:
  button-primary:
    backgroundColor: "{colors.jade}"
    textColor: "{colors.jade-ink}"
    rounded: "{rounded.sm}"
    padding: "0 28px"
    height: "54px"
  button-primary-hover:
    backgroundColor: "{colors.jade-lifted}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.paper-dim}"
    rounded: "{rounded.sm}"
    padding: "12px 22px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.paper-dim}"
    rounded: "{rounded.sm}"
    padding: "0 16px"
    height: "54px"
  chip:
    backgroundColor: "transparent"
    textColor: "{colors.paper-dim}"
    rounded: "{rounded.sm}"
    padding: "10px 18px"
  input:
    backgroundColor: "{colors.graphite-deep}"
    textColor: "{colors.paper}"
    rounded: "{rounded.md}"
    padding: "20px 22px"
  mode-panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.paper}"
    rounded: "{rounded.lg}"
    padding: "30px 30px 24px"
  nav-button:
    backgroundColor: "transparent"
    textColor: "{colors.paper-dim}"
    rounded: "{rounded.sm}"
    padding: "10px 18px"
  material-toggle-active:
    backgroundColor: "{colors.jade}"
    textColor: "{colors.jade-ink}"
    padding: "12px 24px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.paper}"
    rounded: "{rounded.lg}"
---

# Design System: Conjure Kiosk

## 1. Overview

**Creative North Star: "Machine Enamel"**

Conjure is the enamelled housing of a fabrication machine, not an app. The whole surface is a deep green-cast graphite — the color of anodized tooling under low light — and it exists for one reason: so the conjured 3D object, rendered bright and rotating, is the most alive thing on screen. Chrome recedes; geometry commands the eye. This is why the interface is dark. Not because "tools look cool dark," but because *the object is the hero* and the enclosure should disappear around it.

The accent is not chosen; it is *taken*. The jade green (`#45d38a`) is the exact color the Three.js viewer renders the generated mesh. The UI wears the color of the thing it makes. Type splits along a single honest line: **Archivo** carries everything a person reads (invitations, headings, prose), and **IBM Plex Mono** carries everything the machine says — step names, specs, toolchain, timings. If it's a promise to the user, it's Archivo. If it's the machine reporting its own work, it's mono.

This system explicitly rejects four things, straight from the product's anti-references: dense professional CAD chrome (toolbars, gizmos, property inspectors); gimmicky "AI toy" aesthetics (rainbow gradients, gradient text, playful rounded everything); generic SaaS marketing-template landing pages (cream backgrounds, tracked eyebrows on every section, hero-metric blocks, identical icon-card grids); and cluttered maker-forum density (cramped panels, competing status widgets, gray-on-gray). It is a real fabrication tool, calm and legible at arm's length.

**Key Characteristics:**
- One dark green-graphite surface, one jade accent lifted from the rendered geometry.
- Two-font honesty: Archivo speaks to the human, IBM Plex Mono reports the machine.
- Physical, tactile controls — keycap buttons that visibly depress under a finger.
- Kiosk-first legibility: ≥48px touch targets, ≥4.5:1 body contrast, no hover-only affordances.
- Flat by default; depth comes from tonal layering and one machined bottom edge, never drop shadows.

## 2. Colors

A near-monochrome green-graphite field, warmed by paper-white text and cut by a single jade accent — the color of conjured geometry.

### Primary
- **Jade** (`#45d38a`): The color the 3D viewer renders the mesh, borrowed for the UI. Primary buttons, active states, the emphasized word in the hero (`real object`), success, mono kickers on the Make panel, focus rings. Composed in OKLCH as `oklch(.79 .17 158)`, shipped as hex for kiosk-Chromium safety.
- **Jade Lifted** (`#55dd97`): Primary-button hover only — jade with the lights turned up.
- **Jade Dim** (`#2fae6d`): Pressed/active button fill and the progress-bar fill; jade under load.
- **Jade Edge** (`#1e8a52`): The 3px darker bottom edge of every keycap button — the machined lip that makes a press feel physical.
- **Jade Ink** (`#06170e`): The near-black green used for text *on* jade fills. Never used as a surface.

### Neutral — Green Graphite
- **Green Graphite** (`#0f1713`): The body. The enclosure. Everything sits on this.
- **Graphite Deep** (`#0b110e`): The recessed well — text inputs, code viewers, the top nav placard, card thumbnails.
- **Surface** (`#16211c`): Raised panels, cards, secondary buttons.
- **Surface Raised** (`#1c2a23`): The mic rest, hover states one step up from Surface.
- **Surface Lifted** (`#17261f`): The green-tinted hover of the jade-accented Make panel.
- **Border** (`#2a3a32`) / **Border Soft** (`#223029`): Hairline 1px separations. Border for interactive edges, Border Soft for quiet dividers.

### Neutral — Warm Paper (text ramp)
- **Paper** (`#f2f5ef`): Primary text and headings. 16.6:1 on the body.
- **Paper Dim** (`#b7c4ba`): Body prose, descriptions, secondary button text. 10:1.
- **Sage** (`#8ba094`): Mono labels, captions, timings, tertiary detail. 6.6:1 — still clears AA.
- **Sage Faint** (`#5c6f65`): Decorative only (flow-rail arrows, disabled hints). 3.4:1 — **large-text / non-informational use only.**

### Tertiary — Machine Status
- **Amber** (`#e0a458`): Structural-review warnings in Engineer mode. Always paired with text, never color-alone.
- **Red** (`#e5604f`): Errors and review failures. Always paired with an icon or label.

### Named Rules
**The Borrowed-Accent Rule.** The jade accent is defined once as the render color of the generated mesh. If the viewer's model color ever changes, the UI accent changes with it. The interface wears the color of the thing it makes — jade is never a decorative brand choice, it is a readout.

**The Two-Voice Rule.** Green is the machine; paper is the human. A jade or mono element is the machine reporting its own work. An Archivo, paper-colored element is the product speaking to the person. Never blur the two.

## 3. Typography

**Display / UI Font:** Archivo (with -apple-system, system-ui fallback) — self-hosted variable font, weights 400–800.
**Label / Mono Font:** IBM Plex Mono (with ui-monospace, SF Mono, Menlo fallback) — self-hosted, weights 400/500/600.

**Character:** A contrast-axis pairing, not two-of-a-kind. Archivo is a grotesque workhorse — confident, slightly condensed, excellent at large display weights and small UI sizes alike. IBM Plex Mono is the machine's own handwriting: even, technical, unmistakably "this is data." The gap between them *is* the information architecture.

### Hierarchy
- **Display** (Archivo 800, 46px, line-height 1.06, -0.02em): The idle-screen invitation only. One per screen. `text-wrap: balance`.
- **Headline** (Archivo 700, 34px, 1.1, -0.02em): Screen titles ("Describe what you want to build", "Engineering your part").
- **Title** (Archivo 700, 27px, 1.15, -0.015em): Mode-panel names, card headings.
- **Body** (Archivo 400, 17px, 1.55): Prose and descriptions. Capped ~58ch. Paper Dim on the dark surface.
- **Label** (IBM Plex Mono 500, 12px, letter-spacing 0.08em): Mode kickers (`MAKE ·`, `ENGINEER ·`), spec labels, toolchain, step names. Sage.
- **Mono Data** (IBM Plex Mono 400, 13px): Live machine output — step messages, timings, the flow rail, SCAD preview.

### Named Rules
**The Machine-Speaks-Mono Rule.** Anything the machine emits about its own process — step names, retrieved specs, generated SCAD, timings, the toolchain — is IBM Plex Mono. Anything the product says *to* the user is Archivo. The font is the speaker label.

**The One-Kicker Rule.** The small mono all-caps label is permitted exactly where it names a mode or a machine field (`MAKE · voice or keyboard`, `HOW ENGINEER WORKS`). It is forbidden as a decorative eyebrow above every section. If the kicker isn't naming a real thing, delete it.

## 4. Elevation

Flat by default. This system conveys depth through **tonal layering**, not drop shadows: Graphite Deep (wells) → Green Graphite (body) → Surface (panels) → Surface Raised (hover). A panel is "above" the body because it's lighter, not because it casts a shadow. There is no ambient shadow vocabulary; a 2014-style soft drop shadow would read as foreign here.

The single exception to flatness is **physical, not atmospheric**: primary/secondary buttons carry a 3px darker bottom border (`jade-edge` / border) that reads as a machined lip, and depress (`translateY(2px)`, edge shrinks to 1px) on `:active`. Depth you can press, not depth that floats.

### Shadow Vocabulary
- **Mic pulse** (`box-shadow: 0 0 0 6px rgba(69,211,138,0.16), 0 0 0 14px rgba(69,211,138,0.06)`, animated): The *only* glow in the system. Signals active listening on the push-to-talk mic. State feedback, not decoration — silenced under `prefers-reduced-motion`.
- **Viewer scrim** (`linear-gradient(to top, rgba(11,17,14,0.95), transparent)`): Not a shadow — a legibility gradient behind the 3D-viewer action bar so controls stay readable over any rendered geometry.

### Named Rules
**The Flat-Enclosure Rule.** Surfaces are flat at rest. The only permitted depth cues are tonal (a lighter surface) and physical (the keycap bottom edge). Drop shadows and glows are forbidden except the mic's active-listening pulse. If a surface floats, flatten it.

## 5. Components

### Buttons
- **Shape:** Machined, not pillowy — 3px radius (`rounded.sm`). Never fully rounded.
- **Primary (keycap):** Jade fill, Jade-Ink text, `0 28px` padding, 54px tall, with a 3px `jade-edge` bottom border. On `:active` it presses: `translateY(2px)` and the edge shrinks to 1px. Hover lifts to Jade Lifted. Built to be struck by a finger.
- **Secondary:** Surface fill, Paper-Dim text, 1px border with a 3px bottom border (the same keycap language, quieter). Min 48px tall.
- **Ghost:** Transparent, Paper-Dim text → Paper on hover. For Back and low-stakes exits.
- **Hover / Focus:** Color transitions only (`background-color`/`border-color`/`color`, 0.15s ease). Focus-visible draws a 2px jade outline at 2px offset — the global `outline: none` is always paired with this.

### Chips (example prompts)
- **Style:** Transparent fill, 1px Border, Paper-Dim mono text, 3px radius, min 48px tall. Fill to Surface on hover.
- **Behavior:** Tap injects the example text into the adjacent input. Action chips, not filters.

### Cards / Containers
- **Mode panels:** Surface fill, 6px radius (`rounded.lg`), 1px border with a 4px bottom border. The Make panel is edged in translucent jade (`rgba(69,211,138,0.35)`) with a jade bottom edge — the "lit door"; the Engineer panel is plain graphite — the "graphite door." Deliberately differentiated, never an identical card grid.
- **Library / aside cards:** Surface (or transparent for asides), 6px radius, 1px border. No shadow — tonal layering only.
- **Internal padding:** Generous — 24–30px. Nested cards are forbidden.

### Inputs / Fields
- **Style:** Graphite Deep well, 1px Border, 4px radius, Paper text, 16.5px. The prompt composer is Archivo; the Engineer textarea is mono (the user is dictating specs to the machine).
- **Focus:** Border shifts to Jade. No glow.
- **Labeling:** Every field carries an `aria-label`; placeholder is never the only label.

### Navigation
- **Style:** A solid 60px Graphite-Deep placard, no glass, 1px Border-Soft underline. Logo (hexagon + wordmark) left, mono lowercase status center, Home/Library buttons right.
- **States:** Buttons are transparent with a 1px border, Paper-Dim → Paper on hover; min 48px tall.

### Signature Component — The Build Log
The generating screens are a **build log, not a spinner**. Each machine stage is a `step-item`: a hexagon glyph (`⬡`, the logo motif) that fills as the stage completes, an Archivo step name, and a live mono `step-msg`. The whole list is an `aria-live="polite"` region so the minutes-long autonomous pipeline announces its progress. *Progress you can trust* is a component, not a principle: a named, visible stage earns patience; a hidden spinner destroys it.

## 6. Do's and Don'ts

### Do:
- **Do** derive the accent from the rendered geometry. Jade (`#45d38a`) is the viewer's model color; keep them locked.
- **Do** keep the surface dark green-graphite (`#0f1713`) so the rotating object is the brightest thing on screen — *the object is the hero.*
- **Do** use Archivo for the human and IBM Plex Mono for the machine, every time.
- **Do** make primary actions physical keycaps with a bottom edge and a real `:active` press.
- **Do** hold ≥48px touch targets and ≥4.5:1 body contrast; the user is a standing stranger at arm's length.
- **Do** convey depth by tonal layering (Deep → Graphite → Surface → Raised).
- **Do** name every machine stage in the build log and wrap it in `aria-live`.

### Don't:
- **Don't** ship dense CAD chrome — toolbars, gizmos, property inspectors, modal dialogs. One primary action per screen.
- **Don't** use gimmicky "AI toy" aesthetics: rainbow gradients, cartoon mascots, playful rounded-everything, and **never** gradient text (`background-clip: text`).
- **Don't** slip into generic SaaS-landing clichés: cream/warm-neutral backgrounds, tracked eyebrows above every section, hero-metric blocks, identical icon-card grids. The home screen is an orientation surface, not a funnel.
- **Don't** recreate cluttered maker-forum density — cramped panels, competing status widgets, low-contrast gray-on-gray.
- **Don't** add drop shadows or glassmorphism. The only glow permitted is the mic's active-listening pulse.
- **Don't** use Sage Faint (`#5c6f65`, 3.4:1) for anything a user must read — decorative and large-text only.
- **Don't** encode meaning in color alone: amber/red review states always carry text or an icon.
- **Don't** rely on hover to reveal function or state — there is no hover on the kiosk.
