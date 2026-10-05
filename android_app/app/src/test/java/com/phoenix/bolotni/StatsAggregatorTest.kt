package com.phoenix.bolotni

import com.phoenix.bolotni.local.DetectionDao
import com.phoenix.bolotni.local.DetectionEntity
import com.phoenix.bolotni.local.StatsAggregator
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class StatsAggregatorTest {

    private val dummyDao = object : DetectionDao {
        override suspend fun insert(detection: DetectionEntity): Long = 1L
        override suspend fun update(detection: DetectionEntity) {}
        override suspend fun delete(detection: DetectionEntity) {}
        override fun getAllFlow() = throw NotImplementedError()
        override fun getRecentFlow(limit: Int) = throw NotImplementedError()
        override suspend fun getAllList(): List<DetectionEntity> = emptyList()
        override suspend fun getById(id: Long): DetectionEntity? = null
        override suspend fun getByDateRange(startTs: Long, endTs: Long) = emptyList<DetectionEntity>()
        override suspend fun getBySpecies(slug: String) = emptyList<DetectionEntity>()
        override suspend fun getPendingSync(limit: Int) = emptyList<DetectionEntity>()
        override suspend fun markAsSynced(ids: List<Long>) {}
        override suspend fun getTopSpeciesSince(sinceTs: Long) = emptyList<com.phoenix.bolotni.local.SpeciesCount>()
        override suspend fun getHourlyActivitySince(sinceTs: Long) = emptyList<com.phoenix.bolotni.local.HourlyActivity>()
        override suspend fun getTotalCount(): Int = 0
        override suspend fun deleteOlderThan(olderThanTs: Long): Int = 0
    }

    @Test
    fun testExportToCsv() {
        val aggregator = StatsAggregator(dummyDao)
        val detections = listOf(
            DetectionEntity(
                id = 1,
                speciesSlug = "anas_platyrhynchos",
                speciesRu = "Кряква",
                habitat = "wetland",
                confidence = 0.92f,
                votesCount = 3,
                windowStartTs = 1000L,
                windowEndTs = 6000L,
                lat = 55.751244,
                lon = 37.618423,
                createdAt = 1700000000000L
            )
        )

        val csv = aggregator.exportToCsv(detections)
        assertTrue(csv.contains("id,species_slug,species_ru"))
        assertTrue(csv.contains("anas_platyrhynchos"))
        assertTrue(csv.contains("\"Кряква\""))
        assertTrue(csv.contains("55.751244"))
    }

    @Test
    fun testExportToGeoJson() {
        val aggregator = StatsAggregator(dummyDao)
        val detections = listOf(
            DetectionEntity(
                id = 1,
                speciesSlug = "ardea_cinerea",
                speciesRu = "Серая цапля",
                habitat = "wetland",
                confidence = 0.88f,
                votesCount = 2,
                windowStartTs = 2000L,
                windowEndTs = 7000L,
                lat = 56.123,
                lon = 38.456,
                createdAt = 1700000000000L
            )
        )

        val geoJson = aggregator.exportToGeoJson(detections)
        val root = JSONObject(geoJson)
        assertEquals("FeatureCollection", root.getString("type"))
        val features = root.getJSONArray("features")
        assertEquals(1, features.length())
        val f = features.getJSONObject(0)
        assertEquals("Point", f.getJSONObject("geometry").getString("type"))
        val coords = f.getJSONObject("geometry").getJSONArray("coordinates")
        assertEquals(38.456, coords.getDouble(0), 1e-4)
        assertEquals(56.123, coords.getDouble(1), 1e-4)
        assertEquals("ardea_cinerea", f.getJSONObject("properties").getString("species_slug"))
    }
}
