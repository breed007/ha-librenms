# Home Assistant brand assets

Staging area for the [`home-assistant/brands`](https://github.com/home-assistant/brands)
pull request. Home Assistant does not read integration artwork from the
integration itself — it comes from that separate repository — so nothing here
affects the running integration. Until the PR lands, Home Assistant shows the
default puzzle-piece icon for this integration, which is expected.

## Provenance

**These are LibreNMS's marks, not artwork made for this project.** The brands
repository expects an integration to carry the upstream project's own logo so
users recognise it, and inventing a LibreNMS mark would be both wrong and
confusing next to their real branding.

Every PNG here is rendered from the official SVGs published in
[`librenms/librenms`](https://github.com/librenms/librenms/tree/master/html/images):

| Output | Rendered from |
|---|---|
| `icon.png`, `icon@2x.png` | `librenms_logo_only_light.svg` |
| `dark_icon.png`, `dark_icon@2x.png` | `librenms_logo_only_dark.svg` |
| `logo.png`, `logo@2x.png` | `librenms_logo_light.svg` |
| `dark_logo.png`, `dark_logo@2x.png` | `librenms_logo_dark.svg` |

Note the light/dark naming inverts between the two projects, which is easy to
get backwards. LibreNMS names a file for **the background it sits on**, so
`librenms_logo_light.svg` is dark grey artwork meant for a light background.
Home Assistant names a file for **the artwork itself**, where the unprefixed
`logo.png` is the one that must work on a white background. So HA's `logo.png`
comes from LibreNMS's `_light` file, and HA's `dark_logo.png` from their
`_dark` file.

## Specification

Verified against the brands repository requirements:

| File | Dimensions | Constraint |
|---|---|---|
| `icon.png` / `dark_icon.png` | 256×256 | exactly square |
| `icon@2x.png` / `dark_icon@2x.png` | 512×512 | exactly square |
| `logo.png` / `dark_logo.png` | 680×128 | shortest side 128–256 |
| `logo@2x.png` / `dark_logo@2x.png` | 1360×256 | shortest side 256–512 |

All are RGBA PNGs with transparent backgrounds, Adam7 interlaced, losslessly
optimised, and trimmed so the artwork touches the canvas edge.

## Regenerating

Re-run whenever LibreNMS updates their artwork. Sources are downloaded at run
time rather than vendored, so the output always tracks upstream.

```bash
python3 -m venv /tmp/brandtools
/tmp/brandtools/bin/pip install resvg-py pillow pyoxipng
/tmp/brandtools/bin/python scripts/build_brand_assets.py
```

## Submitting the pull request

Blocked until `github.com/breed007/ha-librenms` is public — the brands
repository requires the integration to be publicly available before accepting
a `custom_integrations` entry.

1. Fork and clone `home-assistant/brands`.
2. Copy this directory across, preserving the path:
   ```bash
   cp -R brands/custom_integrations/librenms \
     /path/to/brands/custom_integrations/librenms
   ```
3. From the brands checkout, run their validation: `script/setup` then
   `script/validate`.
4. Open the PR, linking to the public integration repository. The domain
   directory name must stay `librenms`, matching `manifest.json`.
