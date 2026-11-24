plugins { id("com.android.application") }

val buildNumber = (System.getenv("LC4_BUILD") ?: "1").toInt()

android {
    namespace = "com.lightcode4.app"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.lightcode4.app"
        minSdk = 26
        targetSdk = 35
        versionCode = buildNumber
        versionName = "1.0.$buildNumber"
        buildConfigField("int", "LC4_BUILD", buildNumber.toString())
    }
    buildFeatures { buildConfig = true }
    signingConfigs {
        create("sideload") {
            // Releases are signed with the same key every time, so updates install over the previous version.
            storeFile = file(System.getenv("LC4_KEYSTORE") ?: "lightcode4.keystore")
            storePassword = System.getenv("LC4_KEYSTORE_PASS") ?: "lightcode4"
            keyAlias = "lightcode4"
            keyPassword = System.getenv("LC4_KEYSTORE_PASS") ?: "lightcode4"
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("sideload")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
