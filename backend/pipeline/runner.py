from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend.config import Settings
from backend.models.domain import JobSettings, Transcript
from backend.pipeline.alignment import build_atempo_filter, plan_duration_match
from backend.pipeline.audio import (
    commit_media_output,
    cut_reference_audio,
    extract_audio_tracks,
    media_cache_is_valid,
    media_duration,
    select_reference_segment,
)
from backend.pipeline.context import JobPaths
from backend.pipeline.download import download_video
from backend.pipeline.mixer import build_dubbed_timeline, build_separator, mix_audio
from backend.pipeline.muxer import mux_burned_subtitle, mux_soft_subtitle
from backend.pipeline.segmenter import merge_short_segments
from backend.pipeline.subtitle import write_ass, write_srt
from backend.pipeline.translator import (
    Translator,
    build_translator,
    save_translation_cache,
    translate_transcript,
)
from backend.pipeline.tts import CosyVoiceClient, wav_duration
from backend.pipeline.whisper_asr import WhisperAdapter
from backend.utils.files import atomic_copy, atomic_write_json, read_json, temporary_output_path
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)
UpdateCallback = Callable[..., Awaitable[dict[str, Any]]]
LogCallback = Callable[[str, str], Awaitable[None]]
CancelledCallback = Callable[[], bool]


STEPS = (
    "下载/准备视频",
    "提取音频",
    "Whisper 转写",
    "整理字幕",
    "翻译中文",
    "CosyVoice 中文配音",
    "时间轴同步",
    "音轨混合",
    "合成视频",
)


class PipelineCancelled(RuntimeError):
    pass


class PipelineRunner:
    def __init__(self, config: Settings):
        self.config = config

    async def run(
        self,
        job: dict[str, Any],
        *,
        update: UpdateCallback,
        log: LogCallback,
        is_cancelled: CancelledCallback,
    ) -> dict[str, Any]:
        paths = JobPaths.create(Path(job["work_dir"]).resolve().parent, job["id"])
        options = JobSettings.model_validate(job["settings"])
        if not options.target_language.lower().startswith("zh"):
            raise RuntimeError("当前 MVP 只支持目标语言为简体中文")

        def check_cancelled() -> None:
            if is_cancelled():
                raise PipelineCancelled("任务已取消")

        async def step(index: int, message: str | None = None) -> None:
            check_cancelled()
            label = message or STEPS[index - 1]
            await update(
                current_step=f"[{index}/9] {label}",
                step_index=index,
                total_steps=9,
                progress=round((index - 1) / 9 * 100, 2),
            )
            await log(f"[{index}/9] {label}", "INFO")

        async def finish_step(index: int) -> None:
            await update(progress=round(index / 9 * 100, 2))

        await step(1)
        if job["source_kind"] == "youtube":
            video = await download_video(
                job["source_value"],
                paths.source,
                self.config,
                on_log=lambda line: log(line, "INFO"),
            )
        else:
            video = Path(job["source_value"]).expanduser().resolve()
            if not video.is_file():
                raise RuntimeError(f"本地视频不存在：{video}")
        duration = await media_duration(video, self.config)
        await log(f"视频已就绪，时长 {duration:.2f} 秒", "INFO")
        await finish_step(1)

        await step(2)
        speech_audio, original_audio = await extract_audio_tracks(video, paths.audio, self.config)
        await finish_step(2)

        await step(3)
        whisper = WhisperAdapter(self.config)
        transcript = await whisper.transcribe(
            speech_audio,
            paths.asr,
            model=options.whisper_model,
            language=options.source_language,
            on_log=lambda line: log(f"Whisper: {line}", "INFO"),
        )
        if not transcript.segments:
            raise RuntimeError("Whisper 没有识别到可处理的语音片段")
        await update(segments_total=len(transcript.segments), segments_done=0)
        await finish_step(3)

        await step(4)
        segmented_path = paths.asr / "segmented_transcript.json"
        segmented = self._load_transcript(segmented_path)
        if segmented is None:
            segmented = merge_short_segments(transcript)
            segmented.duration = duration
            atomic_write_json(segmented_path, segmented.model_dump())
        await update(segments_total=len(segmented.segments), segments_done=0)
        await log(
            f"字幕片段：Whisper {len(transcript.segments)} 段 → 语义整理 {len(segmented.segments)} 段",
            "INFO",
        )
        await finish_step(4)

        await step(5)
        translator = build_translator(
            self.config,
            options.translation_provider,
            options.translation_model,
        )

        async def translation_progress(done: int, total: int) -> None:
            progress = ((4 + done / max(total, 1)) / 9) * 100
            await update(progress=round(progress, 2))
            await log(f"[TRANSLATE] batch {done}/{total}", "INFO")

        translated = await translate_transcript(
            segmented,
            paths.translation / "translation.json",
            translator,
            provider=options.translation_provider,
            target_language=options.target_language,
            on_progress=translation_progress,
        )
        zh_srt = paths.translation / "zh.srt"
        zh_ass = paths.translation / "zh.ass"
        self._write_chinese_subtitles(translated, zh_srt, zh_ass, options)
        await finish_step(5)

        dubbed_voice: Path | None = None
        if options.dubbing_enabled:
            await step(6)
            reference_audio, reference_text = await self._prepare_reference(
                job,
                options,
                translated,
                speech_audio,
                paths,
            )
            await log(
                f"声音参考：{reference_audio.name}（{len(reference_text)} 字符）",
                "INFO",
            )
            async with CosyVoiceClient(self.config.cosyvoice_url) as client:
                health = await client.health()
                await log(f"CosyVoice 已就绪：{health.get('model', '')}", "INFO")

                async def tts_progress(done: int, total: int, segment_id: int) -> None:
                    check_cancelled()
                    await update(
                        segments_done=done,
                        segments_total=total,
                        progress=round(((5 + done / max(total, 1)) / 9) * 100, 2),
                    )
                    await log(f"[TTS] {done}/{total} segment={segment_id}", "INFO")

                tts_results = await client.synthesize_segments(
                    translated.segments,
                    tts_dir=paths.tts,
                    prompt_audio=reference_audio,
                    prompt_text=reference_text,
                    progress=tts_progress,
                )
                tts_changed = any(not item.cached for item in tts_results)
                tts_changed = await self._repair_durations(
                    translated,
                    translator,
                    client,
                    reference_audio,
                    reference_text,
                    paths,
                    log,
                    check_cancelled,
                ) or tts_changed
            if tts_changed:
                self._invalidate_downstream(paths)
            # Compression retries may have changed subtitle wording.
            save_translation_cache(
                paths.translation / "translation.json",
                translated,
                {
                    segment.id: segment.translated_text or ""
                    for segment in translated.segments
                },
                options.translation_provider,
                translator.model,
                options.target_language,
            )
            self._write_chinese_subtitles(translated, zh_srt, zh_ass, options)
            await finish_step(6)

            await step(7)
            dubbed_voice = await build_dubbed_timeline(
                translated.segments,
                duration,
                paths.mix / "dubbed_voice.wav",
                self.config,
            )
            await finish_step(7)
        else:
            await step(6, "中文配音已关闭，跳过")
            await finish_step(6)
            await step(7, "时间轴同步已跳过")
            await finish_step(7)

        await step(8)
        background: Path | None
        fast_mode = options.separation_mode == "fast"
        if dubbed_voice is None:
            background = original_audio
        elif options.keep_background:
            separator = build_separator(options.separation_mode, self.config)
            background = await separator.separate(original_audio, paths.mix / "separation")
        else:
            background = None
        mixed_audio = await mix_audio(
            background,
            dubbed_voice,
            paths.mix / "mixed.wav",
            duration,
            self.config,
            fast_mode=fast_mode,
        )
        await finish_step(8)

        await step(9)
        final_video = paths.output / "final_zh.mp4"
        soft_subtitle = zh_srt if options.subtitle_mode in {"soft", "both"} else None
        await mux_soft_subtitle(video, mixed_audio, soft_subtitle, final_video, self.config)
        artifacts: list[Path] = [final_video]
        if soft_subtitle is not None:
            soft_alias = paths.output / "final_zh_subtitle.mp4"
            if not await media_cache_is_valid(
                soft_alias,
                self.config,
                required_stream_types={"video", "audio", "subtitle"},
            ):
                atomic_copy(final_video, soft_alias)
            artifacts.append(soft_alias)
        if options.subtitle_mode in {"burn", "both"}:
            burned = await mux_burned_subtitle(
                video,
                mixed_audio,
                zh_ass,
                paths.output / "final_zh_burned.mp4",
                self.config,
                fallback_srt=zh_srt,
                font_name=options.subtitle_font,
            )
            artifacts.append(burned)

        for source in (
            paths.asr / "transcript.json",
            paths.asr / "original.srt",
            paths.translation / "translation.json",
            zh_srt,
            zh_ass,
            dubbed_voice,
            mixed_audio,
        ):
            if source is None or not source.exists():
                continue
            destination = paths.output / source.name
            if source.resolve() != destination.resolve():
                atomic_copy(source, destination)
            artifacts.append(destination)
        await finish_step(9)
        artifact_names = sorted({path.name for path in artifacts if path.exists()})
        return {
            "primary": final_video.name,
            "artifacts": artifact_names,
            "output_dir": str(paths.output),
            "duration": duration,
            "segments": len(translated.segments),
        }

    @staticmethod
    def _load_transcript(path: Path) -> Transcript | None:
        if not path.exists():
            return None
        try:
            return Transcript.model_validate(read_json(path))
        except Exception as exc:
            logger.warning("忽略损坏的分段缓存 %s: %s", path, exc)
            return None

    @staticmethod
    def _write_chinese_subtitles(
        transcript: Transcript,
        srt_path: Path,
        ass_path: Path,
        options: JobSettings,
    ) -> None:
        write_srt(
            transcript,
            srt_path,
            translated=True,
            wrap_chinese=True,
            preserve_ids=False,
        )
        write_ass(
            transcript,
            ass_path,
            translated=True,
            font_name=options.subtitle_font,
        )

    async def _prepare_reference(
        self,
        job: dict[str, Any],
        options: JobSettings,
        transcript: Transcript,
        speech_audio: Path,
        paths: JobPaths,
    ) -> tuple[Path, str]:
        if options.reference_mode == "upload":
            if not job.get("reference_path"):
                raise RuntimeError("声音模式为上传，但没有找到 reference audio")
            path = Path(job["reference_path"]).expanduser().resolve()
            if not path.is_file():
                raise RuntimeError(f"上传的声音参考不存在：{path}")
            if not options.reference_text:
                raise RuntimeError("上传 reference audio 时必须填写它对应的原始文字")
            return path, options.reference_text

        if options.reference_mode == "segment":
            if options.reference_segment_id is None:
                raise RuntimeError("手动声音模式需要 reference segment ID")
            segment = next(
                (item for item in transcript.segments if item.id == options.reference_segment_id),
                None,
            )
            if segment is None:
                raise RuntimeError(f"找不到参考 segment ID：{options.reference_segment_id}")
        else:
            segment = select_reference_segment(transcript)
        reference = await cut_reference_audio(
            speech_audio,
            segment,
            paths.audio / "reference.wav",
            self.config,
        )
        return reference, segment.text

    async def _repair_durations(
        self,
        transcript: Transcript,
        translator: Translator,
        client: CosyVoiceClient,
        reference_audio: Path,
        reference_text: str,
        paths: JobPaths,
        log: LogCallback,
        check_cancelled: Callable[[], None],
    ) -> bool:
        changed = False
        for index, segment in enumerate(transcript.segments, start=1):
            check_cancelled()
            if not segment.tts_file or segment.tts_duration is None:
                raise RuntimeError(f"segment {segment.id} 缺少 TTS 结果")
            plan = plan_duration_match(segment.tts_duration, segment.duration)
            if plan.needs_text_compression and plan.action == "compress_text":
                for compression_round in range(1, 3):
                    compressed = await translator.compress(
                        segment.text,
                        segment.translated_text or "",
                        segment.duration,
                    )
                    if compressed == segment.translated_text:
                        break
                    segment.translated_text = compressed
                    synthesis = await client.synthesize(
                        text=compressed,
                        prompt_audio=reference_audio,
                        prompt_text=reference_text,
                        output_path=Path(segment.tts_file),
                        speed=1.0,
                        overwrite=True,
                    )
                    segment.tts_duration = synthesis.duration
                    changed = True
                    plan = plan_duration_match(synthesis.duration, segment.duration)
                    await log(
                        f"[ALIGN] segment={segment.id} 压缩译文第 {compression_round} 次，"
                        f"target={segment.duration:.2f}s generated={synthesis.duration:.2f}s",
                        "INFO",
                    )
                    if plan.action != "compress_text":
                        break

            # Use CosyVoice speed first for a mild excess, then only a tiny atempo correction.
            plan = plan_duration_match(segment.tts_duration or 0, segment.duration)
            if plan.action == "speed_up" and plan.tts_speed > 1.001:
                synthesis = await client.synthesize(
                    text=segment.translated_text or "",
                    prompt_audio=reference_audio,
                    prompt_text=reference_text,
                    output_path=Path(segment.tts_file),
                    speed=plan.tts_speed,
                    overwrite=True,
                )
                segment.tts_duration = synthesis.duration
                changed = True

            ratio = (segment.tts_duration or 0) / segment.duration
            atempo = min(max(ratio, 1.0), 1.06)
            if atempo > 1.001:
                aligned = paths.tts / f"{segment.id:04d}_aligned.wav"
                await self._apply_atempo(Path(segment.tts_file), aligned, atempo)
                segment.tts_file = str(aligned)
                segment.tts_duration = wav_duration(aligned)
                changed = True
            final_ratio = (segment.tts_duration or 0) / segment.duration
            level = "WARNING" if final_ratio > 1.15 else "INFO"
            await log(
                f"[ALIGN] {index}/{len(transcript.segments)} segment={segment.id} "
                f"target={segment.duration:.2f}s generated={segment.tts_duration:.2f}s "
                f"ratio={final_ratio:.3f}",
                level,
            )
        return changed

    @staticmethod
    def _invalidate_downstream(paths: JobPaths) -> None:
        """Remove generated dependants after a TTS cache entry changes."""

        for path in (
            paths.mix / "dubbed_voice.wav",
            paths.mix / "mixed.wav",
            paths.output / "dubbed_voice.wav",
            paths.output / "mixed.wav",
            paths.output / "final_zh.mp4",
            paths.output / "final_zh_subtitle.mp4",
            paths.output / "final_zh_burned.mp4",
        ):
            path.unlink(missing_ok=True)

    async def _apply_atempo(self, source: Path, output: Path, factor: float) -> None:
        binary = require_executable(self.config.ffmpeg_bin, "ffmpeg")
        temporary = temporary_output_path(output)
        try:
            await run_process(
                [
                    binary,
                    "-y",
                    "-i",
                    str(source),
                    "-af",
                    build_atempo_filter(factor),
                    "-c:a",
                    "pcm_s16le",
                    str(temporary),
                ]
            )
            await commit_media_output(
                temporary,
                output,
                self.config,
                required_stream_types={"audio"},
            )
        finally:
            temporary.unlink(missing_ok=True)
