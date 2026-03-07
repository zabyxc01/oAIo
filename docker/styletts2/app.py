"""StyleTTS2 — Simple Gradio UI for voice prototyping."""

import gradio as gr
import numpy as np
from styletts2 import tts

# Pre-built voice labels → random seeds for consistent speaker identity
VOICES = {
    "f-us-1": 0,
    "f-us-2": 1,
    "f-us-3": 2,
    "f-us-4": 3,
    "m-us-1": 4,
    "m-us-2": 5,
    "m-us-3": 6,
    "m-us-4": 7,
}

print("Loading StyleTTS2 model (first run downloads ~774MB)...")
model = tts.StyleTTS2()
print("Model loaded.")


def generate(
    text: str,
    voice: str,
    ref_audio: str | None,
    diffusion_steps: int,
    alpha: float,
    beta: float,
    embedding_scale: float,
):
    if not text.strip():
        return None

    kwargs = dict(
        text=text,
        diffusion_steps=int(diffusion_steps),
        alpha=alpha,
        beta=beta,
        embedding_scale=embedding_scale,
    )

    if ref_audio:
        # Voice cloning mode — use uploaded reference
        kwargs["target_voice_path"] = ref_audio
    else:
        # Seed-based voice selection for repeatable speaker identity
        seed = VOICES.get(voice, 0)
        np.random.seed(seed)
        import torch
        torch.manual_seed(seed)

    wav = model.inference(**kwargs)

    # Return as (sample_rate, numpy_array) for Gradio Audio
    return (24000, np.array(wav))


with gr.Blocks(title="StyleTTS2", theme=gr.themes.Soft()) as app:
    gr.Markdown("## StyleTTS2 — Voice Prototyping")

    with gr.Row():
        with gr.Column(scale=2):
            text_input = gr.Textbox(
                label="Text",
                placeholder="Type or paste text here...",
                lines=5,
            )
            voice_dropdown = gr.Dropdown(
                choices=list(VOICES.keys()),
                value="f-us-1",
                label="Voice preset",
                info="Ignored when reference audio is provided",
            )
            ref_audio_input = gr.Audio(
                label="Reference audio (optional — enables voice cloning)",
                type="filepath",
            )

        with gr.Column(scale=1):
            diffusion_steps = gr.Slider(
                minimum=1, maximum=20, value=5, step=1,
                label="Diffusion steps",
                info="More = diverse but slower",
            )
            alpha_slider = gr.Slider(
                minimum=0.0, maximum=1.0, value=0.3, step=0.05,
                label="Alpha (timbre)",
                info="0=voice style, 1=text style",
            )
            beta_slider = gr.Slider(
                minimum=0.0, maximum=1.0, value=0.7, step=0.05,
                label="Beta (prosody)",
                info="0=voice style, 1=text style",
            )
            embedding_scale = gr.Slider(
                minimum=0.0, maximum=5.0, value=1.0, step=0.25,
                label="Embedding scale",
                info="Emotional expressiveness",
            )

    generate_btn = gr.Button("Generate", variant="primary", size="lg")
    audio_output = gr.Audio(label="Output", type="numpy")

    generate_btn.click(
        fn=generate,
        inputs=[
            text_input,
            voice_dropdown,
            ref_audio_input,
            diffusion_steps,
            alpha_slider,
            beta_slider,
            embedding_scale,
        ],
        outputs=audio_output,
    )

app.launch(server_name="0.0.0.0", server_port=7870)
