package com.oaio.kira

import android.content.SharedPreferences
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.util.Log
import org.json.JSONObject

/**
 * VoiceInteractionService — registered as the device assistant.
 * When the user triggers the assistant (hardware button, swipe gesture),
 * this service activates and captures voice for Kira.
 *
 * Future: on-device wake word detection ("Hey Kira") via Porcupine or Whisper VAD.
 */
class KiraService : VoiceInteractionService() {

    private var client: CompanionClient? = null
    private var audio: AudioManager? = null
    private val prefs: SharedPreferences by lazy {
        getSharedPreferences("kira_prefs", MODE_PRIVATE)
    }

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "KiraService created")
        audio = AudioManager()
    }

    override fun onReady() {
        super.onReady()
        val host = prefs.getString("host", DEFAULT_HOST) ?: DEFAULT_HOST
        connectToHub(host)
    }

    override fun onDestroy() {
        client?.disconnect()
        audio?.release()
        super.onDestroy()
    }

    fun connectToHub(host: String) {
        client?.disconnect()
        client = CompanionClient(object : CompanionClient.Listener {
            override fun onConnected() {
                Log.i(TAG, "Connected to oAIo")
            }

            override fun onDisconnected() {
                Log.w(TAG, "Disconnected from oAIo")
            }

            override fun onChatResponse(text: String, emotion: JSONObject?, done: Boolean) {
                if (done) {
                    Log.i(TAG, "Kira: $text")
                    // Response will be spoken via TTS audio that follows
                }
            }

            override fun onTtsAudio(audioData: ByteArray, format: String, sampleRate: Int) {
                audio?.queueAudio(audioData, format)
            }

            override fun onSttTranscript(text: String) {
                Log.i(TAG, "STT: $text")
            }

            override fun onStateSync(config: JSONObject) {
                Log.i(TAG, "State sync: $config")
            }
        })
        client?.connect(host)
    }

    fun sendChat(text: String) {
        client?.sendChat(text)
    }

    fun startListening() {
        audio?.startCapture()
    }

    fun stopListeningAndSend() {
        val pcm = audio?.stopCapture() ?: return
        if (pcm.size < AudioManager.SAMPLE_RATE) return // less than 0.5s
        client?.sendAudio(pcm, AudioManager.SAMPLE_RATE)
    }

    companion object {
        private const val TAG = "KiraService"
        const val DEFAULT_HOST = "100.117.188.118:9000"
    }
}
