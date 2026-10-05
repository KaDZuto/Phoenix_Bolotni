package com.phoenix.bolotni.local

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Сущность подтверждённой детекции птицы в локальной базе Room.
 * Поля строго согласованы со схемой таблицы detections серверного проекта.
 */
@Entity(tableName = "detections")
data class DetectionEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,

    @ColumnInfo(name = "species_slug")
    val speciesSlug: String,

    @ColumnInfo(name = "species_ru")
    val speciesRu: String,

    @ColumnInfo(name = "habitat")
    val habitat: String,

    @ColumnInfo(name = "confidence")
    val confidence: Float,

    @ColumnInfo(name = "votes_count")
    val votesCount: Int = 2,

    @ColumnInfo(name = "window_start_ts")
    val windowStartTs: Long,

    @ColumnInfo(name = "window_end_ts")
    val windowEndTs: Long,

    @ColumnInfo(name = "lat")
    val lat: Double? = null,

    @ColumnInfo(name = "lon")
    val lon: Double? = null,

    @ColumnInfo(name = "created_at")
    val createdAt: Long = System.currentTimeMillis(),

    @ColumnInfo(name = "audio_snippet_path")
    val audioSnippetPath: String? = null,

    @ColumnInfo(name = "sync_status")
    val syncStatus: Int = 0, // 0 = pending, 1 = synced, 2 = skipped

    @ColumnInfo(name = "user_corrected_species")
    val userCorrectedSpecies: String? = null,

    @ColumnInfo(name = "notes")
    val notes: String? = null
)
