package com.phoenix.bolotni.model

import org.json.JSONArray
import org.json.JSONObject

/**
 * Конфигурация модели и параметров скользящего окна / препроцессинга.
 * Единый источник истины — совпадает с model_config.json репозитория.
 */
data class ModelConfig(
    val targetSr: Int = 32000,
    val nFft: Int = 1024,
    val hop: Int = 320,
    val nMels: Int = 128,
    val clipSeconds: Float = 5.0f,
    val noiseClassSlug: String = "_noise",
    val confidenceThreshold: Float = 0.6f,
    val windowStrideSec: Float = 2.5f,
    val windowCountForVote: Int = 3,
    val minAgreeWindows: Int = 2,
    val ttaTimeShiftMs: List<Int> = listOf(-200, 0, 200)
) {
    val targetLengthSamples: Int
        get() = (targetSr * clipSeconds).toInt()

    val strideSamples: Int
        get() = (targetSr * windowStrideSec).toInt()

    val numFrames: Int
        get() = targetLengthSamples / hop + 1

    companion object {
        fun fromJson(jsonStr: String): ModelConfig {
            return try {
                val obj = JSONObject(jsonStr)
                val ttaList = mutableListOf<Int>()
                if (obj.has("tta_time_shift_ms")) {
                    val arr = obj.getJSONArray("tta_time_shift_ms")
                    for (i in 0 until arr.length()) {
                        ttaList.add(arr.getInt(i))
                    }
                } else {
                    ttaList.addAll(listOf(-200, 0, 200))
                }

                ModelConfig(
                    targetSr = obj.optInt("target_sr", 32000),
                    nFft = obj.optInt("n_fft", 1024),
                    hop = obj.optInt("hop", 320),
                    nMels = obj.optInt("n_mels", 128),
                    clipSeconds = obj.optDouble("clip_seconds", 5.0).toFloat(),
                    noiseClassSlug = obj.optString("noise_class_slug", "_noise"),
                    confidenceThreshold = obj.optDouble("confidence_threshold", 0.6).toFloat(),
                    windowStrideSec = obj.optDouble("window_stride_sec", 2.5).toFloat(),
                    windowCountForVote = obj.optInt("window_count_for_vote", 3),
                    minAgreeWindows = obj.optInt("min_agree_windows", 2),
                    ttaTimeShiftMs = ttaList
                )
            } catch (e: Exception) {
                ModelConfig()
            }
        }
    }
}
