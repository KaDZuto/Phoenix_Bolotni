package com.phoenix.bolotni.model

/**
 * Сырой результат одного окна (предварительное наблюдение).
 * Не считается достоверным до прохождения многократной проверки.
 */
data class CandidateDetection(
    val windowIndex: Long,
    val timestampMs: Long,
    val speciesSlug: String?,
    val speciesRu: String?,
    val habitat: String,
    val confidence: Float,
    val isConfident: Boolean,
    val reason: String
)

/**
 * Подтверждённая детекция, прошедшая голосование по скользящим окнам.
 * Гарантирует отсутствие ложной уверенности по единичному шумовому выбросу.
 */
data class ConfirmedDetection(
    val windowIndex: Long,
    val timestampMs: Long,
    val speciesSlug: String,
    val speciesRu: String,
    val habitat: String,
    val votesCount: Int,
    val windowsEvaluated: Int,
    val averageConfidence: Float
)

/**
 * Движок многократной проверки детекций (Задача 2 [P0]).
 * Накапливает результаты последних window_count_for_vote окон.
 * Вид подтверждается ТОЛЬКО если набрал не менее min_agree_windows голосов.
 */
class WindowVoteEngine(
    val config: ModelConfig = ModelConfig(),
    private val onConfirmedListener: ((ConfirmedDetection) -> Unit)? = null
) {
    private val windowHistory = ArrayDeque<CandidateDetection>()
    private var lastConfirmedSpecies: String? = null
    private var lastConfirmedWindowIndex: Long = -1L

    /**
     * Сброс истории окон (например, при смене локации или рестарте сервиса).
     */
    fun reset() {
        windowHistory.clear()
        lastConfirmedSpecies = null
        lastConfirmedWindowIndex = -1L
    }

    /**
     * Обработка результата очередного 5-секундного окна.
     * Возвращает пару: (предварительное окно, подтверждённая детекция при наличии).
     */
    fun processWindow(
        windowIndex: Long,
        timestampMs: Long,
        windowResult: WindowResult
    ): Pair<CandidateDetection, ConfirmedDetection?> {
        val top1 = windowResult.predictions.firstOrNull()
        val isDetected = windowResult.status == "detected" && windowResult.confirmedSpecies != null
        val detectedSpecies = windowResult.confirmedSpecies
        val detectedSpeciesRu = windowResult.confirmedSpeciesRu ?: detectedSpecies ?: "Неизвестно"
        val habitat = top1?.habitat ?: "unknown"
        val prob = top1?.probability ?: 0.0f

        val candidate = CandidateDetection(
            windowIndex = windowIndex,
            timestampMs = timestampMs,
            speciesSlug = detectedSpecies,
            speciesRu = detectedSpeciesRu,
            habitat = habitat,
            confidence = prob,
            isConfident = isDetected,
            reason = windowResult.reason
        )

        windowHistory.addLast(candidate)
        while (windowHistory.size > config.windowCountForVote) {
            windowHistory.removeFirst()
        }

        // Подсчёт голосов уверенных детекций за последние N окон
        val votesMap = mutableMapOf<String, Int>()
        val confidenceMap = mutableMapOf<String, MutableList<Float>>()

        for (item in windowHistory) {
            val slug = item.speciesSlug
            if (item.isConfident && slug != null && slug != config.noiseClassSlug) {
                votesMap[slug] = (votesMap[slug] ?: 0) + 1
                confidenceMap.getOrPut(slug) { mutableListOf() }.add(item.confidence)
            }
        }

        var confirmed: ConfirmedDetection? = null

        // Находим вид с максимальным числом голосов
        val topVoted = votesMap.maxByOrNull { it.value }
        if (topVoted != null && topVoted.value >= config.minAgreeWindows) {
            val species = topVoted.key
            val votes = topVoted.value
            val confList = confidenceMap[species] ?: listOf(prob)
            val avgConf = confList.average().toFloat()

            // Предотвращаем дублирование детекций подряд по одним и тем же окнам
            val isNewSpecies = (lastConfirmedSpecies != species)
            val isSufficientGap = (windowIndex - lastConfirmedWindowIndex) >= config.minAgreeWindows

            if (isNewSpecies || isSufficientGap) {
                val matchingCandidate = windowHistory.findLast { it.speciesSlug == species }
                val confRu = matchingCandidate?.speciesRu ?: species
                val confHab = matchingCandidate?.habitat ?: "unknown"

                val confDetection = ConfirmedDetection(
                    windowIndex = windowIndex,
                    timestampMs = timestampMs,
                    speciesSlug = species,
                    speciesRu = confRu,
                    habitat = confHab,
                    votesCount = votes,
                    windowsEvaluated = windowHistory.size,
                    averageConfidence = avgConf
                )

                lastConfirmedSpecies = species
                lastConfirmedWindowIndex = windowIndex
                confirmed = confDetection
                onConfirmedListener?.invoke(confDetection)
            }
        }

        return Pair(candidate, confirmed)
    }
}
