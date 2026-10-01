# Instrument format signatures

These tiny files are safe format examples used to test instrument recognition.
They are **not** production data and the uploader does not read this folder.

The three COM-port instruments are:

- `NO2-CAPS` (`no2_caps.txt`)
- `NEPH-PM25` (`neph_pm25.txt`)
- `CO2-LICOR` (`co2_licor.xml`)

`SMPS` and `BC-MA200` are file-based on the field laptop, so they do not get a
COM-port assignment. Their examples are included because the same pre-Bronze
validator protects all five instrument prefixes. The source archive contained
no black-carbon rows, so `black_carbon_ma200.csv` is an explicitly sanitized
format fixture rather than a scientific measurement.

From the repository directory in VS Code, check these examples with:

```powershell
py -3 scripts\field\identify_instrument_data.py sample_data\instrument_signatures
```

To predict the currently attached COM ports, stop the scheduled serial logger
first (only one process can open a COM port), then run:

```powershell
py -3 scripts\field\acquire_serial.py --detect-ports --probe-seconds 20
```

Detection is report-only. Review the predicted instrument names before saving:

```powershell
py -3 scripts\field\acquire_serial.py --apply-detected-ports --probe-seconds 20
```
