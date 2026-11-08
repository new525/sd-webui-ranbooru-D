# Ranbooru (Forge Neo fork)

![Alt text](pics/ranbooru.png)

> ### ⚠️ This is a fork
> This repository is a fork of **[Inzaniak/sd-webui-ranbooru](https://github.com/Inzaniak/sd-webui-ranbooru)**, maintained to keep the extension working on **[sd-webui-forge-classic (Neo branch)](https://github.com/Haoming02/sd-webui-forge-classic)** and to add quality-of-life features. All original work, design, and credit belong to **Inzaniak**. If you are not specifically on Forge Neo, prefer the upstream extension.
>
> See [Changes from upstream](#changes-from-upstream) for the full list of what this fork adds or fixes.

Ranbooru is an extension for the [AUTOMATIC1111 Stable Diffusion UI](https://github.com/AUTOMATIC1111/stable-diffusion-webui) (and forks). The purpose of this extension is to add a panel that gets a random set of tags from booru pictures. This is mostly used to test checkpoints on a large variety of tags.

![Alt text](pics/image.png)

## Installation
Copy the script from the `scripts` folder into the extensions folder of your WebUI installation (or install this repository as an extension). Then restart the WebUI, or click the "Reload UI" button at the bottom of the page. You should find a new panel called "Ranbooru" at the bottom of the page.

This fork targets **Forge Neo**; upstream's A1111 instructions still apply for the base extension.

## Changes from upstream

This fork diverges from [Inzaniak/sd-webui-ranbooru](https://github.com/Inzaniak/sd-webui-ranbooru) in the following ways. Everything not listed here behaves as upstream documents.

### Forge Neo compatibility
- Rebuilt the Gelbooru fetch path so it never mutates the instance URL between calls (the old in-place append caused duplicated query parameters and, in retry paths, malformed requests).

### New features
- **Filter Character Tags (Gelbooru only)** — strips character-name tags from the selected post by batch-querying Gelbooru's Tag List API (one extra request per generation) and removing tags of type `4` (character).
- **Skip tags already in prompt** — drops fetched tags that already appear in your prompt, with normalised matching (case-insensitive, `_`/space-equivalent, and weight-syntax aware, so `blue_hair` matches `(blue hair:1.2)`).
- **Tag Presets** — up to five named tag-set presets with a "pick a random preset per generation" toggle that overrides the main Tags field, each with an optional **per-preset Max Pages** override (0 = use the global setting).
- **Mix ratio** — controls the split when Mix Prompts is active: `0` = all tags from image 1, `1` = all from image 2, `0.5` = even. For 3+ mix sources the weights interpolate linearly.
- **Mix from presets** — when random presets are enabled, draws the second mix source from a *different* randomly chosen preset's search, so a single generation can blend tags across two preset "worlds."
- **Second LoRA folder (LoRAnado)** — an optional second folder with its own amount, min/max weight, and custom-weights fields, so you can group LoRAs by their ideal weight range and draw from both in one generation.
- **Wider LoRA weight range** — Min/Max LoRA Weight sliders now span **-2.0 to 2.0** (was -1.0 to 1.0) on both folders.
- **Max Pages ceiling raised** to 1000 (was 100), matching what the preset Max Pages fields already allowed.
- **Gelbooru API credentials UI** — enter and optionally save your Gelbooru API key / user ID; saved credentials are hidden in the UI and can be cleared. (Gelbooru has required an API key + user ID since June 2025.)

### Behaviour fixes
- **Empty searches no longer crash and no longer skip LoRAnado.** A search returning no posts — a booru error page with no `post` key, an empty result, or an exhausted page walk-down — now skips only the tag-application step, leaves your prompt as written, and still runs LoRAnado and the rest of the pass. (Previously this raised and aborted.)
- **Index range taken from the fetched page, not the global hit count.** For Gelbooru, the total hit count can far exceed the posts on one page; indices are now drawn from the actual page length, fixing an `IndexError` on small/late pages and the `ValueError`/`NaN` crashes on empty pages.
- **All boorus build request URLs without mutating state.** The base class now builds each request URL from a pristine base and passes credentials via `params=`. This fixes **mix-from-presets on every booru** (previously the two fetches it performs corrupted the URL on the eight non-Gelbooru classes), and removes the duplicated-query-parameter problem generally.
- **Request timeouts everywhere.** Every booru request now has a timeout, so a stalled booru can't hang generation indefinitely.
- **Gelbooru credentials kept out of logs and cache.** `api_key`/`user_id` now travel as request params rather than in the URL string, the credential-bearing URL print was removed, and the request cache ignores those parameters so they aren't written into `ranbooru_cache.sqlite`.
- **Page walk-down re-rolls within the known-good range** instead of linearly decrementing from a possibly-distant page, so an empty page past a booru's result window recovers in a few tries rather than exhausting retries.
- **Mix prompts** takes an equal share from each source post, copies the post dict before mutating tags (fixing a cache-poisoning bug), and filters empty/whitespace tags.

### Smaller fixes
- LoRAnado: invalid custom weights no longer throw, and a degenerate weight range (e.g. min = max = 0) no longer spins forever.
- Deepbooru: when the model is unavailable the modify step is skipped rather than discarding the fetched booru tags.
- Preset selection no longer collapses duplicate preset text to the first entry's Max Pages override.
- Assorted upstream cleanups (e621 class/name and `get_post` return, duplicated `COUNT` line in Yandere).

> **Note on the companion State extension:** if you use `sd-webui-state` to persist panel settings, clear the `state-ext-ranbooru-*` keys from `localStorage` (or re-set the controls once) after updating. These additions inserted new controls into the middle of the panel's control list, which shifts the positional indices State uses; appending them at the end would have avoided the break, and a future version may re-order them to restore compatibility.

---

## Features (from upstream)
The extension is divided into two main functionalities that can be used together or separately.

### Ranbooru
This is the main part of the extension. It gets a random set of tags from booru pictures. Parameters:
- **Booru**: The booru to get tags from. Gelbooru, Rule34, Safebooru, yande.re, konachan, aibooru, danbooru and xbooru are implemented. You can add more by creating a class for the booru and adding it to the booru list in the script.

#### ⚠️ Gelbooru API Credentials Required
As of June 2025, Gelbooru requires an API key and user ID to access its API. When you select Gelbooru in the Ranbooru panel, fields appear to enter your API key and user ID. You can optionally save these credentials to disk; if saved, the input fields are hidden and a message shows the path to the credentials file. You can clear the saved credentials at any time.

- **API Key & User ID (Gelbooru only)**: Required to use Gelbooru. Obtain these from your Gelbooru account settings.
- **Max Pages**: The maximum number of pages to get tags from. The extension gets a random page and then tags from one or more random pictures on that page. *(This fork raises the ceiling to 1000.)*
- **Post ID**: Specify the ID of a post to get tags from. Blank = a random post (or more than one) from the random page.
- **Tags to Search (Pre)**: Tags (comma-separated, e.g. `1girl,solo,short_hair`) added to the search query.
- **Tags to Remove (Post)**: Tags (comma-separated) removed from the result. Use `*` to match any tag containing a word, e.g. `*hair` removes every tag containing "hair".
- **Filter Character Tags (Gelbooru only)** *(fork)*: Removes character-name tags via an extra Tag List API lookup.
- **Skip tags already in prompt** *(fork)*: Doesn't add fetched tags that already appear in your prompt.
- **Mature Rating**: Sets the mature rating (SFW/NSFW). Works on supported boorus (tested on Gelbooru).
- **Remove Bad Tags**: Removes tags you usually don't need (watermarks, text, censor).
- **Shuffle Tags**: Shuffles tags before adding them.
- **Convert "\_" to Spaces**: Converts `_` to spaces in the tags.
- **Use the same prompt for all images**: Uses one prompt for the whole batch.
- **Fringe Benefits**: Gelbooru-only; enables all hidden content (equivalent to "Display all site content").
- **Limit Tags / Max Tags**: Limit the number of tags, by percentage or absolute count.
- **Change Background / Change Color**: Attempt to change background or color by adding/removing specific tags.
- **Sorting Order**: Orders scraped pictures by high or low score (applied after scraping).
- **Use img2img / Send to controlnet / Denoising Strength / Use last image as img2img / Crop Center**: img2img and ControlNet handoff options.
- **Use Deepbooru**: Uses the Deepbooru model to tag the random image instead of its existing tags.
- **Use tags_search.txt / tags_remove.txt (+ choosers)**: Use tag lists from files in the extension's `user/search` and `user/remove` folders.
- **Mix Prompts / Mix Amount**: Mix tags from several random images. *(This fork adds **Mix ratio** and **Mix from presets**.)*
- **Chaos Mode / Chaos Amount / Negative Mode**: Move tags between positive and negative prompts.
- **Use Same Seed / Use Cache**: Reuse seed across a batch; cache fetched pages.

#### Tag Presets *(fork)*
Up to five named presets. With "pick a random preset per generation" enabled, one preset's tags override the Tags field each generation. Each preset has an optional Max Pages override (0 = global).

### LoRAnado
Pick random LoRAs from a folder and add them to the prompt.
- **Lock Previous LoRAs**: Reuse the previous generation's LoRAs.
- **LoRAs Subfolder**: The subfolder to use (required).
- **LoRAs Amount**: How many LoRAs to use.
- **Min / Max LoRAs Weight**: Weight range. *(This fork widens the range to -2.0 … 2.0.)*
- **LoRAs Custom Weights**: Explicit comma-separated weights (e.g. `0.2,0.3,0.5`); blank = use min/max.
- **Second LoRA folder** *(fork)*: An optional second folder with its own amount, weight range, and custom weights.

## How to use
See [usage.md](usage.md) for a detailed explanation, and Inzaniak's guide on [CivitAI](https://civitai.com/articles/3357/ranbooru-the-comprehensive-guide).

## Known Issues
- Chaos mode and negative mode can error with batch size > 1 combined with batch count > 1. Rerunning usually fixes it.
- `sd-dynamic-prompts` conflicts with the multiple-prompts option; disabling it is the only known fix.
- img2img currently creates a 1-step image before the real one.
- Send to controlnet needs a dummy image selected in the ControlNet panel.

## Credits

**Original extension by [Inzaniak](https://github.com/Inzaniak).** This fork only adds Forge Neo compatibility and the features listed under [Changes from upstream](#changes-from-upstream); all core design and the overwhelming majority of the code are Inzaniak's.

Upstream special thanks to [TheGameratorT](https://github.com/TheGameratorT), [SmashinFries](https://github.com/SmashinFries), and [w-e-w](https://github.com/w-e-w) for contributing.

Support Inzaniak's work:
- **Patreon**: https://www.patreon.com/Inzaniak
- **Personal Website**: https://inzaniak.github.io
- **DeviantArt**: https://www.deviantart.com/inzaniak
- **CivitAI**: https://civitai.com/user/Inzaniak/models

Also check Inzaniak's other scripts:
- [Ranbooru for ComfyUI](https://github.com/Inzaniak/comfyui-ranbooru)
- [Workflow](https://github.com/Inzaniak/sd-webui-workflow)

## License
**Upstream has no LICENSE file.** Without a license, default copyright applies to the original work and no redistribution rights are granted — so, strictly, neither upstream nor this fork is clearly licensed for reuse. This fork claims no rights over Inzaniak's original work and exists only as a compatibility/feature fork. If you intend to rely on or redistribute this, consider asking [Inzaniak](https://github.com/Inzaniak) to add an explicit license upstream. (Not legal advice.)
