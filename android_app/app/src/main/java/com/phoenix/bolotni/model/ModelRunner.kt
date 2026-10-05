package com.phoenix.bolotni.model

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.nio.FloatBuffer
import kotlin.math.exp
import kotlin.math.max

data class SpeciesInfo(
    val slug: String,
    val ruName: String,
    val habitat: String
)

data class Prediction(
    val speciesSlug: String,
    val speciesRu: String,
    val habitat: String,
    val probability: Float
)

data class WindowResult(
    val status: String, // "detected" или "unknown/uncertain"
    val confirmedSpecies: String?,
    val confirmedSpeciesRu: String?,
    val reason: String,
    val top1Species: String,
    val top1SpeciesRu: String,
    val top1Probability: Float,
    val predictions: List<Prediction>
)

/**
 * Раннер модели на базе ONNX Runtime Mobile.
 * Поддерживает полноразмерные и квантованные (int8) ONNX модели, динамическую подмену весов и TTA.
 */
class ModelRunner(
    var config: ModelConfig = ModelConfig(),
    var preprocessor: AudioPreprocessor = AudioPreprocessor(config)
) {
    private var ortEnv: OrtEnvironment? = null
    private var ortSession: OrtSession? = null
    private var inputName: String = "input"

    private val classesList = mutableListOf<String>()
    private val speciesMetadata = mutableMapOf<String, SpeciesInfo>()

    val isInitialized: Boolean
        get() = ortSession != null

    val totalClasses: Int
        get() = classesList.size

    fun getSpeciesInfo(slug: String): SpeciesInfo {
        return speciesMetadata[slug] ?: SpeciesInfo(
            slug = slug,
            ruName = if (slug == config.noiseClassSlug) "Фоновый шум" else slug,
            habitat = "unknown"
        )
    }

    /**
     * Инициализация из ассетов приложения.
     */
    fun initializeFromAssets(
        context: Context,
        modelAssetPath: String = "bird_model.onnx",
        classesAssetPath: String = "classes.json",
        whitelistAssetPath: String = "ru_birds_whitelist.json",
        configAssetPath: String = "model_config.json"
    ) {
        val configStr = readAssetString(context, configAssetPath)
        if (configStr != null) {
            updateConfig(ModelConfig.fromJson(configStr))
        }

        val classesStr = readAssetString(context, classesAssetPath)
        if (classesStr != null) {
            loadClasses(classesStr)
        }

        val whitelistStr = readAssetString(context, whitelistAssetPath)
        if (whitelistStr != null) {
            loadSpeciesWhitelist(whitelistStr)
        }

        val modelBytes = context.assets.open(modelAssetPath).use { it.readBytes() }
        initializeOrt(modelBytes)
    }

    /**
     * Инициализация из локальных файлов (для динамического обновления Задачи 6).
     */
    fun initializeFromFiles(
        modelFile: File,
        classesFile: File,
        whitelistFile: File,
        configFile: File
    ) {
        if (configFile.exists()) {
            updateConfig(ModelConfig.fromJson(configFile.readText(Charsets.UTF_8)))
        }
        if (classesFile.exists()) {
            loadClasses(classesFile.readText(Charsets.UTF_8))
        }
        if (whitelistFile.exists()) {
            loadSpeciesWhitelist(whitelistFile.readText(Charsets.UTF_8))
        }
        val modelBytes = modelFile.readBytes()
        initializeOrt(modelBytes)
    }

    fun updateConfig(newConfig: ModelConfig) {
        this.config = newConfig
        this.preprocessor = AudioPreprocessor(newConfig)
    }

    fun loadClasses(classesJsonStr: String) {
        classesList.clear()
        val arr = JSONArray(classesJsonStr)
        for (i in 0 until arr.length()) {
            classesList.add(arr.getString(i))
        }
    }

    fun loadSpeciesWhitelist(whitelistJsonStr: String) {
        speciesMetadata.clear()
        val obj = JSONObject(whitelistJsonStr)
        for (key in obj.keys()) {
            val item = obj.getJSONObject(key)
            val ru = item.optString("ru", key)
            val habitat = item.optString("habitat", "unknown")
            speciesMetadata[key] = SpeciesInfo(slug = key, ruName = ru, habitat = habitat)
        }
    }

    private fun initializeOrt(modelBytes: ByteArray) {
        close()
        ortEnv = OrtEnvironment.getEnvironment()
        val sessionOptions = OrtSession.SessionOptions().apply {
            setIntraOpNumThreads(2)
        }
        ortSession = ortEnv!!.createSession(modelBytes, sessionOptions)
        inputName = ortSession!!.inputNames.iterator().next()
    }

    /**
     * Инференс одного 5-секундного аудио-окна.
     * Возвращает результат с проверкой порога уверенности и отсечкой фонового шума.
     */
    fun predictWindow(
        audio: FloatArray,
        confThreshold: Float? = null,
        topK: Int = 3,
        useTta: Boolean = false
    ): WindowResult {
        val session = ortSession ?: throw IllegalStateException("ONNX модель не загружена")
        val env = ortEnv ?: throw IllegalStateException("OrtEnvironment не инициализирован")

        val threshold = confThreshold ?: config.confidenceThreshold
        val shifts = if (useTta) config.ttaTimeShiftMs else listOf(0)
        val numClasses = classesList.size

        val accumulatedProbs = FloatArray(numClasses)

        for (shiftMs in shifts) {
            val features = preprocessor.processToLogMel(audio, timeShiftMs = shiftMs)
            val numFrames = config.numFrames
            val tensorShape = longArrayOf(1, 3, config.nMels.toLong(), numFrames.toLong())

            val floatBuffer = FloatBuffer.wrap(features)
            val inputTensor = OnnxTensor.createTensor(env, floatBuffer, tensorShape)

            val results = session.run(mapOf(inputName to inputTensor))
            val rawOutput = results[0].value

            val logits: FloatArray = when (rawOutput) {
                is Array<*> -> {
                    val firstRow = rawOutput[0]
                    when (firstRow) {
                        is FloatArray -> firstRow
                        is Array<*> -> (firstRow as Array<Float>).toFloatArray()
                        else -> FloatArray(numClasses)
                    }
                }
                is FloatArray -> rawOutput
                else -> FloatArray(numClasses)
            }

            val probs = softmax(logits)
            for (i in 0 until minOf(probs.size, numClasses)) {
                accumulatedProbs[i] += probs[i]
            }

            inputTensor.close()
            results.close()
        }

        // Усреднение предсказаний по TTA
        for (i in 0 until numClasses) {
            accumulatedProbs[i] /= shifts.size.toFloat()
        }

        // Ранжирование по вероятности
        val indexedProbs = accumulatedProbs.mapIndexed { idx, prob -> Pair(idx, prob) }
            .sortedByDescending { it.second }

        val predictions = mutableListOf<Prediction>()
        val limit = minOf(topK, indexedProbs.size)
        for (k in 0 until limit) {
            val (clsIdx, prob) = indexedProbs[k]
            val slug = if (clsIdx < classesList.size) classesList[clsIdx] else "class_$clsIdx"
            val info = getSpeciesInfo(slug)
            predictions.add(
                Prediction(
                    speciesSlug = slug,
                    speciesRu = info.ruName,
                    habitat = info.habitat,
                    probability = prob
                )
            )
        }

        val top1 = predictions.firstOrNull() ?: Prediction(
            speciesSlug = "none",
            speciesRu = "Неизвестно",
            habitat = "unknown",
            probability = 0.0f
        )

        val status: String
        val confirmedSpecies: String?
        val confirmedSpeciesRu: String?
        val reason: String

        if (top1.speciesSlug == config.noiseClassSlug) {
            status = "unknown/uncertain"
            reason = "Top-1 распознан как фоновый класс (${config.noiseClassSlug})"
            confirmedSpecies = null
            confirmedSpeciesRu = null
        } else if (top1.probability < threshold) {
            status = "unknown/uncertain"
            reason = "Уверенность %.2f ниже порога %.2f".format(top1.probability, threshold)
            confirmedSpecies = null
            confirmedSpeciesRu = null
        } else {
            status = "detected"
            reason = "Уверенность выше порога"
            confirmedSpecies = top1.speciesSlug
            confirmedSpeciesRu = top1.speciesRu
        }

        return WindowResult(
            status = status,
            confirmedSpecies = confirmedSpecies,
            confirmedSpeciesRu = confirmedSpeciesRu,
            reason = reason,
            top1Species = top1.speciesSlug,
            top1SpeciesRu = top1.speciesRu,
            top1Probability = top1.probability,
            predictions = predictions
        )
    }

    private fun softmax(logits: FloatArray): FloatArray {
        var maxVal = Float.NEGATIVE_INFINITY
        for (v in logits) {
            if (v > maxVal) maxVal = v
        }
        var sum = 0.0f
        val expBuf = FloatArray(logits.size)
        for (i in logits.indices) {
            val e = exp(logits[i] - maxVal)
            expBuf[i] = e
            sum += e
        }
        if (sum > 0.0f) {
            for (i in expBuf.indices) {
                expBuf[i] /= sum
            }
        }
        return expBuf
    }

    private fun readAssetString(context: Context, path: String): String? {
        return try {
            context.assets.open(path).use { it.bufferedReader().readText() }
        } catch (e: Exception) {
            null
        }
    }

    fun close() {
        ortSession?.close()
        ortSession = null
        ortEnv?.close()
        ortEnv = null
    }
}
