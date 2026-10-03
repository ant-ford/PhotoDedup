"""Zero-shot image classification with OpenCLIP, run locally on the CPU.

The model weights are downloaded once on first use (about 600 MB) and cached by
open_clip; images never leave this computer.
"""

from __future__ import annotations

import numpy as np

# Several phrasings per category; their text embeddings are averaged.
# Junk prompts stress "graphic" and overlaid text so ordinary photos that happen to
# contain text (a team banner, a Christmas jumper) still match "photo" best.
PROMPTS = {
    "photo": [
        "a personal photo of people", "a family photo", "a photo of a baby", "a photo of young children",
        "a candid photo of kids playing at home", "a photo of a child in fancy dress", "a selfie",
        "a group photo at an event", "a photo of a sports team", "a family photo at Christmas",
        "a holiday photo of a place", "a landscape photo", "a photo of people outdoors",
        "a photo of a pet", "a photo of food on a table", "a close-up photo of a hand",
    ],
    "screenshot": [
        "a screenshot of a phone screen", "a screenshot of a chat conversation",
        "a screenshot of a mobile app", "a screenshot of a website", "a screenshot of a social media post",
    ],
    "meme": [
        "an internet meme with bold caption text", "a meme image with white text at the top and bottom",
        "a cartoon comic strip joke", "a funny edited picture shared on social media",
    ],
    "advert": [
        "an advertisement graphic with a logo and prices", "a promotional flyer with lots of text",
        "a poster advertising an event with dates", "a product promotion banner", "a restaurant menu or promotion",
    ],
    "greeting": [
        "a digital greeting card with decorative text", "a good morning message image with flowers",
        "an inspirational quote on a plain background", "a happy birthday graphic with balloons",
        "a festive season's greetings e-card", "a religious blessing image with text",
        "a cartoon sticker on a plain background",
    ],
    "document": [
        "a photo of a document", "a receipt", "a ticket", "a printed form", "a page of text", "a whiteboard",
    ],
    "news": ["a screenshot of a news article", "a news graphic with a headline", "an infographic with charts"],
}


class ClipClassifier:
    def __init__(self, model_name: str = "ViT-B-32", pretrained: str = "laion2b_s34b_b79k", threads: int = 0):
        import open_clip
        import torch

        if threads:
            torch.set_num_threads(threads)
        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
        self.model.eval()
        tokenizer = open_clip.get_tokenizer(model_name)

        self.categories = list(PROMPTS)
        with torch.no_grad():
            rows = []
            for cat in self.categories:
                t = self.model.encode_text(tokenizer(PROMPTS[cat]))
                t = t / t.norm(dim=-1, keepdim=True)
                mean = t.mean(dim=0)
                rows.append(mean / mean.norm())
            self.text = torch.stack(rows)

    def embed(self, images) -> np.ndarray:
        """Unit-length image embeddings, one row per PIL image."""
        torch = self.torch
        with torch.no_grad():
            batch = torch.stack([self.preprocess(im) for im in images])
            e = self.model.encode_image(batch)
            e = e / e.norm(dim=-1, keepdim=True)
        return e.cpu().numpy().astype(np.float32)

    def probabilities(self, embeddings: np.ndarray) -> list[dict[str, float]]:
        logits = 100.0 * embeddings @ self.text.cpu().numpy().T
        logits -= logits.max(axis=1, keepdims=True)
        p = np.exp(logits)
        p /= p.sum(axis=1, keepdims=True)
        return [dict(zip(self.categories, map(float, row))) for row in p]
