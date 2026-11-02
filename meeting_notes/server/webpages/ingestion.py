"""Settings navigation and the ingestion pipeline chart."""

from __future__ import annotations

import html

from .shell import _icon


# -- settings ---------------------------------------------------------------


# The five Settings pages (id, title): the left list. Ingestion follows the pipeline: audio, transcribe, identify
# speakers, label speakers, generate notes, name from introductions (see _INGESTION_STEPS).
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
    ("notes", "Generate notes", "sparkles"),
    ("intro", "Name from introductions", "users"),
)


def _ingestion_chart_html(settings) -> str:
    """The flow chart: six linked steps. Statuses and the greyed-out state are first rendered here from the saved
    settings; ``_INGESTION_JS`` keeps them in step with the form while it is edited."""
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
    status = {
        "audio": "Echo removal " + ("on" if settings.echo_removal != "off" else "off"),
        "transcribe": f"GPU helper: {settings.gpu_model}" if helper else f"This server: {settings.model}",
        "speakers": "On" if diarization else "Off",
        "label": "Voice profiles on" if voice else "Off",
        "notes": f"{provider}, automatic for {auto} of {note_types_total} note types" if notes_on else "Off",
    }
    intro_on = bool(settings.intro_naming) and voice and notes_on
    status["intro"] = "On, after the notes" if intro_on else "Off"
    off = {"speakers": not diarization, "label": not voice, "notes": not notes_on, "intro": not intro_on}
    items = []
    for number, (gid, name, icon) in enumerate(_INGESTION_STEPS, 1):
        cls = "ing-step is-off" if off.get(gid) else "ing-step"
        items.append(
            f'<li><a class="{cls}" href="#ingestion/{gid}" data-group="{gid}">'
            f'<span class="ing-ic">{_icon(icon, 18)}</span>'
            f'<span class="ing-name"><span class="sr-only">Step {number}: </span>{name}</span>'
            f'<span class="ing-status" id="ing-status-{gid}">{html.escape(status[gid])}</span></a></li>'
        )
    return (
        '<nav class="ing-flow-wrap" data-page="ingestion" aria-label="Ingestion pipeline">'
        '<p class="help ing-intro">Every recording goes through these steps in this order. Select a step to jump to its settings. '
        'Greyed steps are switched off.</p>'
        f'<ol class="ing-flow">{"".join(items)}</ol></nav>'
    )

_SETTINGS_NAV_HTML = "".join(f'<a href="#{pid}" data-page="{pid}">{title}</a>' for pid, title in _SETTINGS_PAGES)

# Keeps the ingestion chart's status lines and greyed-out steps in step with the form as it is edited (before saving).
_INGESTION_JS = r"""
(function () {
  var form = document.getElementById("settings-form");
  if (!form || !document.querySelector(".ing-flow")) return;
  var PROVIDERS = {codex: "ChatGPT", claude: "Claude", ollama: "Ollama"};
  function el(id) { return document.getElementById(id); }
  function chosen(id) { var s = el(id); return s && s.selectedIndex >= 0 ? s.options[s.selectedIndex].value : ""; }
  function checked(id) { var c = el(id); return !!(c && c.checked); }
  function set(group, text, off) {
    var status = el("ing-status-" + group), step = document.querySelector('.ing-step[data-group="' + group + '"]');
    if (status) status.textContent = text;
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
    set("speakers", diar ? "On" : "Off", !diar);
    set("label", voice ? "Voice profiles on" : "Off", !voice);
    set("notes", provider && provider !== "disabled"
      ? (PROVIDERS[provider] || provider) + ", automatic for " + auto + " of " + boxes.length + " note types" : "Off",
      !provider || provider === "disabled");
    var intro = checked("intro-naming") && voice && !!provider && provider !== "disabled";
    set("intro", intro ? "On, after the notes" : "Off", !intro);
  }
  form.addEventListener("change", update);
  form.addEventListener("input", update);
  update();
})();
"""
