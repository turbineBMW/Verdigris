"""HFP Hands-Free calls — take and place iPhone calls on the Linux desktop.

Call control runs through oFono (org.ofono on the system bus); call audio
(SCO) is carried by PipeWire's oFono HFP backend. Confirmed end-to-end
against iPhone 16 Pro Max / iOS 26.5 — see spike/05b_hfp_ofono.py.
"""
