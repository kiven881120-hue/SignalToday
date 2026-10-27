# Backup And Restore

Backup automation is scheduled for a later stage. The approved policy is documented here so Stage 1 storage choices do not conflict with it.

- PostgreSQL online data remains on the local SSD.
- Local database backups run every six hours and sync to the NAS every six hours.
- Encrypted offsite backup runs daily.
- The NAS is never an online PostgreSQL data directory.
- Target RPO is six hours when a local backup or NAS is available, and 24 hours for a site-level disaster requiring offsite recovery.
- Target RTO is four hours.
