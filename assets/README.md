# Brand assets

The main README expects two images here:

| File | Size | Use |
|---|---|---|
| `banner.gif` | 640×360, animated | README header |
| `banner.png` | 1280×633, under 1 MB | GitHub social preview (Settings → General → Social preview) |
| `logo.png` | 512×512 | Avatar and small placements |

The prompts below were written for an image model (such as Nano Banana). Image models often misspell words, so also generate a **text-free** version, and add the text yourself in Figma, Canva or similar if needed. Keep anything important inside the central 1100×560 area, because GitHub crops the social preview.

## Logo prompt

> Design a minimalist flat vector logo mark for an open-source developer tool called "orch". The concept: an orchestrator that coordinates two AI agents (one plans and reviews, one builds) and only accepts work backed by evidence. Visual idea: a conductor's baton drawn as a single clean stroke that sweeps into a closed loop arrow, suggesting the cycle plan → build → verify → review, with a small check-mark notch where the loop closes. Style: geometric, bold, rounded line caps, uniform stroke weight, flat colours, no text or letters. Palette: deep indigo (#1E1B4B) for the mark, warm amber (#F59E0B) only on the check notch. Centred on a plain white background with generous padding, square 1:1, 1024×1024. It must stay recognisable at 32 pixels. Avoid robots, brains, circuit boards, light bulbs, gears, sparkles, and anything resembling the Anthropic, Claude, Google or Gemini logos.

**Dark-mode variant:** the same mark in white and amber on a transparent background, suitable for #0D1117.

## Banner prompt

> A wide 1280×640 (2:1) banner for the GitHub repository of "orch", an open-source command-line tool that coordinates AI coding agents. Background: a very dark, subtle diagonal gradient from #0D1117 to #1E1B4B. Left side: the orch logo mark, a conductor's baton sweeping into a loop arrow with an amber check notch. Right side: a minimal abstract flow diagram in thin, softly glowing indigo lines. Three rounded nodes in a loop: a blueprint or compass icon (plan), a code-brackets icon (build), and a magnifying glass over a checklist (review). One amber check-mark node marks the exit of the loop. Large title "orch" in a clean geometric sans-serif (in the spirit of Inter or Space Grotesk), white. Beneath it, in light grey, the tagline "Claude plans and reviews. Antigravity builds. Evidence decides." Flat vector style, crisp edges, lots of negative space. No photorealism, no people, no robots, no third-party logos.

**Text-free variant:** the same composition with no text at all, leaving clean space for the title and tagline.
