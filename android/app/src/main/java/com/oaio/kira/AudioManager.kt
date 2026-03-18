package com.oaio.kira

import android.media.*
import android.util.Log
import java.io.ByteArrayInputStream
import java.util.concurrent.LinkedBlockingQueue

/**
 * Handles audio capture (mic → PCM) and playback (WAV/MP3 → speaker).
 */
class AudioManager {
    companion object {
        private const val TAG = "AudioManager"
        const val SAMPLE_RATE = 16000
        private const val CHANNEL_IN = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
    }

    private var recorder: AudioRecord? = null
    private var isRecording = false
    private val capturedData = mutableListOf<ByteArray>()

    private var playbackTrack: AudioTrack? = null
    private val playbackQueue = LinkedBlockingQueue<Pair<ByteArray, String>>()
    private var isPlaying = false
    @Volatile private var playbackThread: Thread? = null

    /** Start capturing audio from the microphone. */
    fun startCapture() {
        val bufSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_IN, ENCODING)
        recorder = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE, CHANNEL_IN, ENCODING,
            bufSize * 2
        )
        capturedData.clear()
        recorder?.startRecording()
        isRecording = true

        Thread {
            val buf = ByteArray(bufSize)
            while (isRecording) {
                val read = recorder?.read(buf, 0, buf.size) ?: -1
                if (read > 0) {
                    synchronized(capturedData) {
                        capturedData.add(buf.copyOf(read))
                    }
                }
            }
        }.start()

        Log.i(TAG, "Capture started")
    }

    /** Stop capture and return all captured PCM data. */
    fun stopCapture(): ByteArray {
        isRecording = false
        recorder?.stop()
        recorder?.release()
        recorder = null

        val total = synchronized(capturedData) {
            val size = capturedData.sumOf { it.size }
            val result = ByteArray(size)
            var offset = 0
            for (chunk in capturedData) {
                System.arraycopy(chunk, 0, result, offset, chunk.size)
                offset += chunk.size
            }
            capturedData.clear()
            result
        }
        Log.i(TAG, "Capture stopped: ${total.size} bytes")
        return total
    }

    /** Queue audio for playback. Plays sequentially. */
    fun queueAudio(data: ByteArray, format: String) {
        playbackQueue.add(Pair(data, format))
        if (!isPlaying) startPlaybackLoop()
    }

    private fun startPlaybackLoop() {
        isPlaying = true
        playbackThread = Thread {
            while (isPlaying) {
                val (data, format) = playbackQueue.poll() ?: run {
                    isPlaying = false
                    return@Thread
                }
                try {
                    if (format == "mp3") {
                        playMp3(data)
                    } else {
                        playWav(data)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Playback error: ${e.message}")
                }
            }
        }.also { it.start() }
    }

    private fun playWav(data: ByteArray) {
        // Parse WAV header to get sample rate
        if (data.size < 44) return
        val sampleRate = (data[24].toInt() and 0xFF) or
                ((data[25].toInt() and 0xFF) shl 8) or
                ((data[26].toInt() and 0xFF) shl 16) or
                ((data[27].toInt() and 0xFF) shl 24)
        val pcmData = data.copyOfRange(44, data.size)

        val bufSize = AudioTrack.getMinBufferSize(
            sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build())
            .setAudioFormat(AudioFormat.Builder()
                .setSampleRate(sampleRate)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .build())
            .setBufferSizeInBytes(maxOf(bufSize, pcmData.size))
            .setTransferMode(AudioTrack.MODE_STATIC)
            .build()

        track.write(pcmData, 0, pcmData.size)
        track.play()
        // Wait for playback to finish
        val durationMs = (pcmData.size.toLong() * 1000) / (sampleRate * 2)
        Thread.sleep(durationMs + 100)
        track.stop()
        track.release()
    }

    private fun playMp3(data: ByteArray) {
        val tempFile = java.io.File.createTempFile("kira_tts", ".mp3")
        tempFile.writeBytes(data)
        val player = MediaPlayer()
        player.setDataSource(tempFile.absolutePath)
        player.prepare()
        player.start()
        while (player.isPlaying) Thread.sleep(50)
        player.release()
        tempFile.delete()
    }

    fun stopPlayback() {
        isPlaying = false
        playbackQueue.clear()
        playbackTrack?.stop()
        playbackTrack?.release()
        playbackTrack = null
    }

    fun release() {
        stopCapture()
        stopPlayback()
    }
}
