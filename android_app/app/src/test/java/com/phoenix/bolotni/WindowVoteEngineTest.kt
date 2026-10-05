package com.phoenix.bolotni

import com.phoenix.bolotni.model.ModelConfig
import com.phoenix.bolotni.model.Prediction
import com.phoenix.bolotni.model.WindowResult
import com.phoenix.bolotni.model.WindowVoteEngine
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class WindowVoteEngineTest {

    private fun makeResult(
        status: String,
        species: String?,
        speciesRu: String?,
        prob: Float,
        habitat: String = "wetland"
    ): WindowResult {
        return WindowResult(
            status = status,
            confirmedSpecies = species,
            confirmedSpeciesRu = speciesRu,
            reason = "test",
            top1Species = species ?: "_noise",
            top1SpeciesRu = speciesRu ?: "Фоновый шум",
            top1Probability = prob,
            predictions = listOf(
                Prediction(
                    speciesSlug = species ?: "_noise",
                    speciesRu = speciesRu ?: "Фоновый шум",
                    habitat = habitat,
                    probability = prob
                )
            )
        )
    }

    /**
     * Критерий приёмки Задачи 2 [P0]:
     * Синтетический тест:
     * 1) Одиночный случайный всплеск (одно окно выше порога, затем шум) — НЕ подтверждается.
     * 2) Устойчивое присутствие вида (2 из 3 окон выше порога) — успешно подтверждается.
     */
    @Test
    fun testSingleRandomSpikeNotConfirmed() {
        val engine = WindowVoteEngine(
            ModelConfig(
                windowCountForVote = 3,
                minAgreeWindows = 2,
                confidenceThreshold = 0.6f
            )
        )

        // Окно 0: Фоновый шум
        val r0 = makeResult("unknown/uncertain", null, null, 0.2f)
        val (_, c0) = engine.processWindow(0L, 0L, r0)
        assertNull("Окно 0 не должно подтверждать детекцию", c0)

        // Окно 1: Случайный всплеск уверенности (ложная тревога / щелчок)
        val r1 = makeResult("detected", "anas_platyrhynchos", "Кряква", 0.95f)
        val (cand1, c1) = engine.processWindow(1L, 2500L, r1)
        assertEquals("anas_platyrhynchos", cand1.speciesSlug)
        assertNull("Единичный случайный всплеск не должен подтверждаться", c1)

        // Окно 2: Снова тишина / шум
        val r2 = makeResult("unknown/uncertain", null, null, 0.1f)
        val (_, c2) = engine.processWindow(2L, 5000L, r2)
        assertNull("Окно 2 не должно подтверждать детекцию", c2)

        // Окно 3: Снова шум
        val r3 = makeResult("unknown/uncertain", null, null, 0.1f)
        val (_, c3) = engine.processWindow(3L, 7500L, r3)
        assertNull("Окно 3 не должно подтверждать детекцию", c3)
    }

    @Test
    fun testSustainedPresenceConfirmed() {
        val engine = WindowVoteEngine(
            ModelConfig(
                windowCountForVote = 3,
                minAgreeWindows = 2,
                confidenceThreshold = 0.6f
            )
        )

        // Окно 0: Начало пения птицы
        val r0 = makeResult("detected", "luscinia_luscinia", "Обыкновенный соловей", 0.85f, "forest")
        val (_, c0) = engine.processWindow(0L, 0L, r0)
        assertNull("Первое окно пока только кандидат", c0)

        // Окно 1: Продолжение пения (второе окно подтверждает)
        val r1 = makeResult("detected", "luscinia_luscinia", "Обыкновенный соловей", 0.88f, "forest")
        val (_, c1) = engine.processWindow(1L, 2500L, r1)
        assertNotNull("Устойчивое присутствие (2 из 3) должно быть подтверждено", c1)
        assertEquals("luscinia_luscinia", c1!!.speciesSlug)
        assertEquals("Обыкновенный соловей", c1.speciesRu)
        assertEquals(2, c1.votesCount)
    }
}
