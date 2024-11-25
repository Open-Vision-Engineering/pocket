import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import torch
import whisper
from pyannote.audio import Pipeline
from pydub import AudioSegment

# Step 1: Load diarization pipeline
pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization", use_auth_token="")

# Step 2: Apply diarization to the audio file
diarization = pipeline("comic_con.wav")

# Load the audio file using pydub
audio = AudioSegment.from_wav("comic_con.wav")

# Step 3: Initialize the Whisper model
model = whisper.load_model("base")

# Step 4: Open a text file to write the output
with open("transcript.txt", "w") as f:
    # Write a header
    f.write("Transcript with Speaker Diarization\n")
    f.write("================================\n\n")
    
    # Iterate through each speaker's segment, transcribe it, and write to file
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        # Extract the corresponding audio segment
        segment = audio[turn.start * 1000: turn.end * 1000]
        
        # Save this segment to a temporary file
        segment.export("temp_segment.wav", format="wav")
        
        # Transcribe the segment using Whisper
        result = model.transcribe("temp_segment.wav")
        
        # Write the transcription with speaker info to file
        f.write(f"[{speaker}] ({turn.start:.1f}s - {turn.end:.1f}s): {result['text']}\n")

print("Transcription complete! Check transcript.txt for the output.")
