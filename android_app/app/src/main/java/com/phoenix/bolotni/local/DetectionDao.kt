package com.phoenix.bolotni.local

import androidx.room.*
import kotlinx.coroutines.flow.Flow

data class SpeciesCount(
    @ColumnInfo(name = "species_slug") val speciesSlug: String,
    @ColumnInfo(name = "species_ru") val speciesRu: String,
    @ColumnInfo(name = "count") val count: Int
)

data class HourlyActivity(
    @ColumnInfo(name = "hour") val hour: Int,
    @ColumnInfo(name = "count") val count: Int
)

@Dao
interface DetectionDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(detection: DetectionEntity): Long

    @Update
    suspend fun update(detection: DetectionEntity)

    @Delete
    suspend fun delete(detection: DetectionEntity)

    @Query("SELECT * FROM detections ORDER BY created_at DESC")
    fun getAllFlow(): Flow<List<DetectionEntity>>

    @Query("SELECT * FROM detections ORDER BY created_at DESC LIMIT :limit")
    fun getRecentFlow(limit: Int = 50): Flow<List<DetectionEntity>>

    @Query("SELECT * FROM detections ORDER BY created_at DESC")
    suspend fun getAllList(): List<DetectionEntity>

    @Query("SELECT * FROM detections WHERE id = :id")
    suspend fun getById(id: Long): DetectionEntity?

    @Query("SELECT * FROM detections WHERE created_at BETWEEN :startTs AND :endTs ORDER BY created_at DESC")
    suspend fun getByDateRange(startTs: Long, endTs: Long): List<DetectionEntity>

    @Query("SELECT * FROM detections WHERE species_slug = :slug ORDER BY created_at DESC")
    suspend fun getBySpecies(slug: String): List<DetectionEntity>

    @Query("SELECT * FROM detections WHERE sync_status = 0 ORDER BY created_at ASC LIMIT :limit")
    suspend fun getPendingSync(limit: Int = 100): List<DetectionEntity>

    @Query("UPDATE detections SET sync_status = 1 WHERE id IN (:ids)")
    suspend fun markAsSynced(ids: List<Long>)

    @Query("""
        SELECT species_slug, species_ru, COUNT(*) as count 
        FROM detections 
        WHERE created_at >= :sinceTs 
        GROUP BY species_slug 
        ORDER BY count DESC
    """)
    suspend fun getTopSpeciesSince(sinceTs: Long): List<SpeciesCount>

    @Query("""
        SELECT (created_at / 3600000) % 24 as hour, COUNT(*) as count 
        FROM detections 
        WHERE created_at >= :sinceTs 
        GROUP BY hour 
        ORDER BY hour ASC
    """)
    suspend fun getHourlyActivitySince(sinceTs: Long): List<HourlyActivity>

    @Query("SELECT COUNT(*) FROM detections")
    suspend fun getTotalCount(): Int

    @Query("DELETE FROM detections WHERE created_at < :olderThanTs")
    suspend fun deleteOlderThan(olderThanTs: Long): Int
}
