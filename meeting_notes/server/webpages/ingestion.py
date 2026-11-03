"""Settings navigation and the ingestion pipeline chart."""

from __future__ import annotations

import html

from .shell import _icon


# -- settings ---------------------------------------------------------------


# The five Settings pages (id, title): the left list. Ingestion follows the pipeline: audio, transcribe, identify
# speakers, label speakers, name from introductions, generate notes (see _INGESTION_STEPS).
_SETTINGS_PAGES = (
    ("general", "General"),
    ("ingestion", "Ingestion"),
    ("gpu", "GPU helpers"),
    ("access", "Notion &amp; AI access"),
    ("recorders", "Recorders &amp; install"),
)
# Old page ids and the Ingestion group they now open (`#transcription`, `#notes` keep working).
_SETTINGS_PAGE_ALIASES = {"transcription": "ingestion/transcribe", "notes": "ingestion/notes"}

# The ingestion pipeline, left to right: (group id, name, icon). The chart at the top of Settings > Ingestion and the
# settings groups below it use the same ids.
_INGESTION_STEPS = (
    ("audio", "Audio", "mic"),
    ("transcribe", "Transcribe", "notes"),
    ("speakers", "Identify speakers", "split"),
    ("label", "Label speakers", "users"),
    ("intro", "Name from intros", "users"),  # short: it fits a narrow step (the settings group says it in full)
    ("notes", "Generate notes", "sparkles"),
)


def _ingestion_chart_html(settings) -> str:
    """The flow chart: six linked steps, each with an On / Off chip in its top right corner and, where there is more
    to say, a detail line (model, provider, ...). Chips, details and the greyed-out state are first rendered here from
    the saved settings; ``_INGESTION_JS`` keeps them in step with the form while it is edited."""
    try:
        note_types_total = len(settings.all_templates())
    except Exception:  # noqa: BLE001
        note_types_total = len(settings.auto_notes_types or [])
    diarization = bool(settings.diarization_enabled)
    voice = bool(settings.voice_profiles_enabled) and diarization
    notes_on = settings.ai_provider != "disabled"
    provider = {"codex": "ChatGPT", "claude": "Claude", "ollama": "Ollama"}.get(settings.ai_provider, settings.ai_provider)
    auto = len(settings.auto_notes_types or [])
    helper = bool(settings.compute_offload_enabled or settings.compute_never_local)
    intro_on = bool(settings.intro_naming) and voice and notes_on
    off = {"speakers": not diarization, "label": not voice, "intro": not intro_on, "notes": not notes_on}
    detail = {
        "audio": "Echo removal " + ("on" if settings.echo_removal != "off" else "off"),
        "transcribe": f"GPU helper: {settings.gpu_model}" if helper else f"This server: {settings.model}",
        "speakers": "",
        "label": "Voice profiles" if voice else "",
        "intro": "Before the notes" if intro_on else "",
        "notes": f"{provider}, automatic for {auto} of {note_types_total} note types" if notes_on else "",
    }
    items = []
    for number, (gid, name, icon) in enumerate(_INGESTION_STEPS, 1):
        is_off = bool(off.get(gid))
        cls = "ing-step is-off" if is_off else "ing-step"
        items.append(
            f'<li><a class="{cls}" href="#ingestion/{gid}" data-group="{gid}">'
            f'<span class="ing-top"><span class="ing-ic">{_icon(icon, 18)}</span>'
            f'<span class="ing-chip{"" if is_off else " on"}" id="ing-chip-{gid}"><span class="dot" aria-hidden="true"></span>'
            f'<span class="ing-chip-text">{"Off" if is_off else "On"}</span></span></span>'
            f'<span class="ing-name"><span class="sr-only">Step {number}: </span>{name}</span>'
            f'<span class="ing-status" id="ing-status-{gid}"{"" if detail[gid] else " hidden"}>'
            f'{html.escape(detail[gid])}</span></a></li>'
        )
    return (
        '<nav class="ing-flow-wrap" data-page="ingestion" aria-label="Ingestion pipeline">'
        '<p class="help ing-intro">Every recording goes through these steps in this order. Select a step to jump to its settings. '
        'Greyed steps are switched off.</p>'
        f'<ol class="ing-flow">{"".join(items)}</ol></nav>'
    )

_SETTINGS_NAV_HTML = "".join(f'<a href="#{pid}" data-page="{pid}">{title}</a>' for pid, title in _SETTINGS_PAGES)

# Keeps the ingestion chart's On / Off chips, detail lines and greyed-out steps in step with the form as it is
# edited (before saving).
_INGESTION_JS = r"""
(function () {
  var form = document.getElementById("settings-form");
  if (!form || !document.querySelector(".ing-flow")) return;
  var PROVIDERS = {codex: "ChatGPT", claude: "Claude", ollama: "Ollama"};
  function el(id) { return document.getElementById(id); }
  function chosen(id) { var s = el(id); return s && s.selectedIndex >= 0 ? s.options[s.selectedIndex].value : ""; }
  function checked(id) { var c = el(id); return !!(c && c.checked); }
  function set(group, text, off) {
    var status = el("ing-status-" + group), chip = el("ing-chip-" + group);
    var step = document.querySelector('.ing-step[data-group="' + group + '"]');
    if (status) { status.textContent = off ? "" : text; status.hidden = off || !text; }
    if (chip) { chip.classList.toggle("on", !off); chip.querySelector(".ing-chip-text").textContent = off ? "Off" : "On"; }
    if (step) step.classList.toggle("is-off", !!off);
  }
  function update() {
    var diar = checked("gpu-speakers"), voice = checked("voice-profiles") && diar, provider = chosen("ai-provider");
    var helper = checked("gpu-offload") || checked("gpu-never-local");
    var model = helper ? chosen("gpu-model") : (form.elements["model"] ? form.elements["model"].value : "");
    var boxes = document.querySelectorAll(".style-auto-notes"), auto = 0;
    Array.prototype.forEach.call(boxes, function (b) { if (b.checked) auto++; });
    set("audio", "Echo removal " + (chosen("echo-removal") === "off" ? "off" : "on"), false);
    set("transcribe", (helper ? "GPU helper: " : "This server: ") + model, false);
    set("speakers", "", !diar);
    set("label", "Voice profiles", !voice);
    var intro = checked("intro-naming") && voice && !!provider && provider !== "disabled";
    set("intro", "Before the notes", !intro);
    set("notes", (PROVIDERS[provider] || provider) + ", automatic for " + auto + " of " + boxes.length + " note types",
      !provider || provider === "disabled");
  }
  form.addEventListener("change", update);
  form.addEventListener("input", update);
  update();
})();
"""
