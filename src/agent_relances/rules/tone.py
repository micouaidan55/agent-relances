"""Ton cible d'une relance, selon le palier et le profil du client (spec §3.7)."""

from agent_relances.models import Profile, Stage, Tone

TONES = {
    (Stage.COURTEOUS, Profile.GOOD): Tone.LIGHT,
    (Stage.COURTEOUS, Profile.OCCASIONAL): Tone.LIGHT,
    (Stage.COURTEOUS, Profile.CHRONIC): Tone.NEUTRAL,
    (Stage.FIRM, Profile.GOOD): Tone.NEUTRAL,
    (Stage.FIRM, Profile.OCCASIONAL): Tone.NEUTRAL,
    (Stage.FIRM, Profile.CHRONIC): Tone.FIRM,
    (Stage.FINAL, Profile.GOOD): Tone.FIRM,
    (Stage.FINAL, Profile.OCCASIONAL): Tone.FIRM,
    (Stage.FINAL, Profile.CHRONIC): Tone.FIRM,
}


def target_tone(stage: Stage, profile: Profile) -> Tone:
    return TONES[(Stage(stage), Profile(profile))]
