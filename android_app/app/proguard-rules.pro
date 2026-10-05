# ProGuard rules for Phoenix Bolotni

# Keep ONNX Runtime classes
-keep class ai.onnxruntime.** { *; }

# Keep Room database classes
-keep class androidx.room.** { *; }

# Keep data models
-keep class com.phoenix.bolotni.local.** { *; }
-keep class com.phoenix.bolotni.model.** { *; }
