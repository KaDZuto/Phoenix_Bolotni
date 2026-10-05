package com.phoenix.bolotni.local

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

/**
 * Локальная база данных Room.
 * Работает 100% офлайн без интернета.
 */
@Database(
    entities = [DetectionEntity::class],
    version = 1,
    exportSchema = false
)
abstract class DetectionsDatabase : RoomDatabase() {

    abstract fun detectionDao(): DetectionDao

    companion object {
        @Volatile
        private var INSTANCE: DetectionsDatabase? = null

        fun getInstance(context: Context): DetectionsDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    DetectionsDatabase::class.java,
                    "phoenix_bolotni_detections.db"
                )
                    .fallbackToDestructiveMigration()
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
