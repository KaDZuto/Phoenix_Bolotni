package com.phoenix.bolotni.local

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

data class HabitatCount(
    val habitat: String,
    val count: Int
)

data class StatsSummary(
    val totalDetections: Int,
    val uniqueSpeciesCount: Int,
    val topSpecies: List<SpeciesCount>,
    val habitatDistribution: List<HabitatCount>,
    val hourlyActivity: List<HourlyActivity>
)

/**
 * Офлайн-агрегатор статистики и экспортер в форматы CSV и GeoJSON (Задача 7 [P0]).
 * Позволяет экологу анализировать и выгружать наблюдения в полевых условиях без интернета.
 */
class StatsAggregator(private val dao: DetectionDao) {

    private val isoDateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)

    suspend fun getSummary(sinceTs: Long = 0L): StatsSummary {
        val detections = if (sinceTs <= 0L) {
            dao.getAllList()
        } else {
            dao.getByDateRange(sinceTs, System.currentTimeMillis())
        }

        val topSpecies = dao.getTopSpeciesSince(sinceTs)
        val hourly = dao.getHourlyActivitySince(sinceTs)

        val habitatMap = mutableMapOf<String, Int>()
        val speciesSet = mutableSetOf<String>()

        for (d in detections) {
            speciesSet.add(d.speciesSlug)
            val h = if (d.habitat.isNotBlank()) d.habitat else "unknown"
            habitatMap[h] = (habitatMap[h] ?: 0) + 1
        }

        val habitatList = habitatMap.map { HabitatCount(it.key, it.value) }.sortedByDescending { it.count }

        return StatsSummary(
            totalDetections = detections.size,
            uniqueSpeciesCount = speciesSet.size,
            topSpecies = topSpecies,
            habitatDistribution = habitatList,
            hourlyActivity = hourly
        )
    }

    /**
     * Экспорт списка детекций в стандартный CSV формат.
     */
    fun exportToCsv(detections: List<DetectionEntity>): String {
        val sb = StringBuilder()
        sb.append("id,species_slug,species_ru,habitat,confidence,votes_count,window_start_ts,window_end_ts,lat,lon,created_at_iso,user_corrected_species\n")

        for (d in detections) {
            val isoDate = isoDateFormat.format(Date(d.createdAt))
            val escapedRu = escapeCsv(d.speciesRu)
            val corrected = escapeCsv(d.userCorrectedSpecies ?: "")
            sb.append("${d.id},${d.speciesSlug},\"$escapedRu\",${d.habitat},${"%.3f".format(Locale.US, d.confidence)},${d.votesCount},${d.windowStartTs},${d.windowEndTs},${d.lat ?: ""},${d.lon ?: ""},\"$isoDate\",\"$corrected\"\n")
        }
        return sb.toString()
    }

    /**
     * Экспорт геопривязанных детекций в формат GeoJSON FeatureCollection.
     */
    fun exportToGeoJson(detections: List<DetectionEntity>): String {
        val root = JSONObject()
        root.put("type", "FeatureCollection")

        val features = JSONArray()
        for (d in detections) {
            val feature = JSONObject()
            feature.put("type", "Feature")

            if (d.lat != null && d.lon != null) {
                val geom = JSONObject()
                geom.put("type", "Point")
                val coords = JSONArray()
                coords.put(d.lon)
                coords.put(d.lat)
                geom.put("coordinates", coords)
                feature.put("geometry", geom)
            } else {
                feature.put("geometry", JSONObject.NULL)
            }

            val props = JSONObject()
            props.put("id", d.id)
            props.put("species_slug", d.speciesSlug)
            props.put("species_ru", d.speciesRu)
            props.put("habitat", d.habitat)
            props.put("confidence", d.confidence)
            props.put("votes_count", d.votesCount)
            props.put("created_at", isoDateFormat.format(Date(d.createdAt)))
            if (d.userCorrectedSpecies != null) {
                props.put("user_corrected_species", d.userCorrectedSpecies)
            }
            feature.put("properties", props)

            features.put(feature)
        }
        root.put("features", features)
        return root.toString(2)
    }

    /**
     * Сохранение экспортированных данных в локальный файл во внутреннем хранилище.
     */
    fun saveExportFile(context: Context, filename: String, content: String): File {
        val exportDir = File(context.filesDir, "exports")
        if (!exportDir.exists()) {
            exportDir.mkdirs()
        }
        val file = File(exportDir, filename)
        file.writeText(content, Charsets.UTF_8)
        return file
    }

    private fun escapeCsv(str: String): String {
        return str.replace("\"", "\"\"")
    }
}
