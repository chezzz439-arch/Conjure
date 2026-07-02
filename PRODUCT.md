# Product

## Register

product

## Users

Two audiences share one touchscreen surface.

**Walk-up kiosk users** — anyone at an event, lab, library, or makerspace who steps up to the Conjure Kiosk (an Orange Pi 5 Pro with a 1080p touchscreen running Chromium in fullscreen kiosk mode). They are standing, often first-time, with no CAD experience and a short attention span. Their job: describe an object out loud or by typing, watch it become a real 3D model, and walk away with a print-ready file on a USB drive — without touching modeling software.

**Makers and light engineers** — the same device, "Engineer" mode. They want a *functional* part (bracket, mount, clip, enclosure) built from real-world dimensions and material specs, not a decorative mesh. Their job: state the requirement in plain language and trust that the researched specs and parametric CAD are sound enough to print and use.

Operating context: single shared device, touch-first (no reliable keyboard/mouse, no hover), variable ambient light, viewed at roughly arm's length. Long async pipelines (AI generation, slicing) run between taps, so waiting is a first-class state.

## Product Purpose

Conjure takes a person from a spoken or typed description to a physical 3D-printed object with no human in the loop after the first button press. The kiosk transcribes voice, generates a 3D model (Meshy for creative shapes, a research-driven OpenSCAD pipeline for engineered parts), shows it in an interactive Three.js viewer, slices it for a Neptune 4 Pro, and writes the G-code to USB — narrating each stage aloud via TTS.

Success is a stranger who has never opened CAD software walking away holding (or about to print) a real object, having trusted the machine the whole way. The interface's job is to make a genuinely complex, minutes-long autonomous pipeline feel effortless, legible, and worth the wait.

## Brand Personality

"Speak it. Build it. Hold it." Effortless, precise, alive.

- **Voice:** plain-spoken and encouraging, never jargon. It says "Describe what you want to build," not "Enter generation parameters." It names each machine step honestly (You.com research, Nebius CAD, CuraEngine slice) so the process feels transparent and earned, not magical hand-waving.
- **Tone:** calm confidence. The dark canvas stays quiet so the generated object — rendered bright and rotating — is the most alive thing on screen. Wonder at the moment of generation; reassurance during the wait; celebration at "ready to print."
- **Emotional goals:** zero intimidation for the first-timer, trust for the engineer, and a small spark of delight when words turn into geometry.

## Anti-references

- **Dense professional CAD software** (Fusion 360, FreeCAD, SolidWorks): toolbars, ribbons, gizmos, modal dialogs, property inspectors. Conjure is the opposite — one primary action per screen, no parameters exposed unless they carry meaning.
- **Gimmicky "AI toy" aesthetics**: rainbow gradients, cartoon mascots, playful rounded everything, gradient text. This is a real fabrication tool, not a novelty.
- **Generic SaaS marketing-template landing pages**: cream/warm-neutral backgrounds, tiny tracked eyebrows above every section, hero-metric blocks, identical icon-card grids. The home screen is an orientation surface for a kiosk, not a funnel.
- **Cluttered maker-forum / hobbyist-dashboard density**: cramped panels, competing status widgets, low-contrast gray-on-gray. Conjure keeps one calm dark surface with a single focal point.

## Design Principles

- **Kiosk-first legibility.** Everything must read at arm's length under uneven light and be tappable by a standing stranger. Generous hit targets, honest contrast on the dark surface, one unmistakable primary action per screen. No hover-only affordances.
- **The object is the hero.** The dark canvas exists so the generated 3D model is the brightest, most alive thing on screen. Chrome recedes; geometry commands the eye. This is why the app is dark, not because "tools look cool dark."
- **Progress you can trust.** Every long, autonomous step is visible and named as it happens. A minutes-long pipeline that shows its work (retrieved specs, generated SCAD, structural review) earns patience and builds trust; a hidden spinner destroys it.
- **Plain language, one thing at a time.** No CAD vocabulary, no multi-object prompts, no configuration the user didn't ask for. Describe → generate → inspect → print, in that order, each step self-explaining.
- **Consistency over surprise.** Two modes (Speak/type, Engineer) share one component vocabulary, one accent, one motion grammar. Delight is reserved for moments — the reveal of the rotating model — not sprinkled across every surface.

## Accessibility & Inclusion

- Target **WCAG 2.1 AA**. Body and informational text must clear 4.5:1 on the dark surfaces (the tertiary gray ramp is the known risk area); large text and UI labels clear 3:1.
- **Touch-first:** interactive controls should meet a comfortable touch target (≈44px) since the primary input is a finger on a shared screen; never rely on hover to reveal function or state.
- **Visible focus** for any keyboard/switch access (the global `outline: none` must be paired with a `:focus-visible` ring).
- **Reduced motion:** honor `prefers-reduced-motion` — the mic pulse, spinners, and transitions need a calm/instant alternative.
- **Don't encode meaning in color alone:** the green "success" and amber/red review states must also carry text or icon cues (they largely do — keep it that way).
