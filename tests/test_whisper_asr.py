from backend.pipeline.whisper_asr import WhisperAdapter


def test_whisper_json_is_normalized_with_word_timestamps():
    transcript = WhisperAdapter._normalize(
        {
            "language": "en",
            "segments": [
                {
                    "id": 4,
                    "start": 1.0,
                    "end": 3.5,
                    "text": " Hello world. ",
                    "avg_logprob": -0.1,
                    "words": [
                        {"word": " Hello", "start": 1.0, "end": 1.7, "probability": 0.95},
                        {"word": " world.", "start": 1.7, "end": 3.4, "probability": 0.9},
                    ],
                }
            ],
        }
    )
    assert transcript.language == "en"
    assert transcript.duration == 3.5
    assert transcript.segments[0].text == "Hello world."
    assert transcript.segments[0].words[1].end == 3.4
    assert 0.9 < transcript.segments[0].confidence < 1.0
