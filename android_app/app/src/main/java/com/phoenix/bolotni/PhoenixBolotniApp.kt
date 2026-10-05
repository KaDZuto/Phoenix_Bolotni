package com.phoenix.bolotni

import android.app.Application
import com.phoenix.bolotni.local.DetectionsDatabase

class PhoenixBolotniApp : Application() {

    lateinit var database: DetectionsDatabase
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        database = DetectionsDatabase.getInstance(this)
    }

    companion object {
        lateinit var instance: PhoenixBolotniApp
            private set
    }
}
