# Assets

Icons, artwork, and UI assets live here.

## Generation

All icons and artwork can be generated via ComfyUI (Flux.1-dev).
Prompts and workflows for asset generation stored in `assets/prompts/`.

## Structure

```
assets/
  icons/          ← node icons, mode icons, service icons
  artwork/        ← boot images, template thumbnails, backgrounds
  prompts/        ← ComfyUI prompts used to generate each asset
  fonts/          ← typography
```

## Regenerating assets

1. Start ComfyUI (MODE 6 — RENDER: IMAGE)
2. Load prompt from assets/prompts/
3. Generate → save output to appropriate folder
4. Restart oAIo UI to pick up new assets
