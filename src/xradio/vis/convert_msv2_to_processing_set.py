# from numcodecs.zstd import Zstd
import numcodecs
from typing import Dict, List, Tuple, Union
import itertools
import json
import numbers
from xradio.vis._vis_utils._ms.partitions import (
    finalize_partitions,
    read_ms_ddi_partitions,
    read_ms_scan_subscan_partitions,
    make_spw_names_by_ddi,
    make_partition_ids_by_ddi_intent,
    make_partition_ids_by_ddi_scan,
)

import dask
from xradio.vis._vis_utils._ms.descr import describe_ms
from xradio.vis._vis_utils._ms.msv2_msv3 import ignore_msv2_cols
from xradio.vis._vis_utils._ms._tables.read import (
    read_generic_table,
    make_freq_attrs,
    convert_casacore_time,
    extract_table_attributes,
)
from xradio.vis._vis_utils._ms._tables.read_main_table import (
    read_flat_main_table,
    read_expanded_main_table,
    get_baselines,
    get_utimes_tol,
    read_main_table_chunks,
)
from xradio.vis._vis_utils._ms.subtables import (
    subt_rename_ids,
    add_pointing_to_partition,
)
from xradio.vis._vis_utils._ms._tables.table_query import open_table_ro, open_query
from xradio.vis._vis_utils._utils.stokes_types import stokes_types
import numpy as np
from casacore import tables
from itertools import cycle
import logging
import time
import xarray as xr


def add_encoding(xds, compressor, chunks=None):
    if chunks is None:
        chunks = xds.dims

    chunks = {**dict(xds.dims), **chunks}  # Add missing dims if presents.

    encoding = {}
    for da_name in list(xds.data_vars):
        if chunks:
            da_chunks = [chunks[dim_name] for dim_name in xds[da_name].dims]
            xds[da_name].encoding = {"compressor": compressor, "chunks": da_chunks}
            # print(xds[da_name].encoding)
        else:
            xds[da_name].encoding = {"compressor": compressor}


def calc_indx_for_row_split(tb_tool, taql_where):
    baselines = get_baselines(tb_tool)
    col_names = tb_tool.colnames()
    cshapes = [
        np.array(tb_tool.getcell(col, 0)).shape
        for col in col_names
        if tb_tool.iscelldefined(col, 0)
    ]

    freq_cnt, pol_cnt = [(cc[0], cc[1]) for cc in cshapes if len(cc) == 2][0]
    utimes, tol = get_utimes_tol(tb_tool, taql_where)
    # utimes = np.unique(tb_tool.getcol("TIME"))

    tvars = {}

    # chunks = [len(utimes), len(baselines), freq_cnt, pol_cnt]

    # print("nrows",  len(tb_tool.getcol("TIME")))

    tidxs = np.searchsorted(utimes, tb_tool.getcol("TIME"))

    ts_ant1, ts_ant2 = (
        tb_tool.getcol("ANTENNA1"),
        tb_tool.getcol("ANTENNA2"),
    )

    ts_bases = [
        str(ll[0]).zfill(3) + "_" + str(ll[1]).zfill(3)
        for ll in np.hstack([ts_ant1[:, None], ts_ant2[:, None]])
    ]
    bidxs = np.searchsorted(baselines, ts_bases)

    # some antenna 2"s will be out of bounds for this chunk, store rows that are in bounds
    didxs = np.where((bidxs >= 0) & (bidxs < len(baselines)))[0]

    baseline_ant1_id, baseline_ant2_id = np.array(
        [tuple(map(int, x.split("_"))) for x in baselines]
    ).T
    return (
        tidxs,
        bidxs,
        didxs,
        baseline_ant1_id,
        baseline_ant2_id,
        convert_casacore_time(utimes, False),
    )


def _check_single_field(tb_tool):
    field_id = np.unique(tb_tool.getcol("FIELD_ID"))
    # print(np.unique(field_id))
    assert len(field_id) == 1, "More than one field present."
    return field_id[0]


def _check_interval_consistent(tb_tool):
    interval = np.unique(tb_tool.getcol("INTERVAL"))
    assert len(interval) == 1, "Interval is not consistent."
    return interval[0]


def read_col(
    tb_tool,
    col: str,
    cshape: Tuple[int],
    tidxs: np.ndarray,
    bidxs: np.ndarray,
    didxs: np.ndarray,
):
    start = time.time()
    data = tb_tool.getcol(col)
    # logging.info("Time to get col " + col + "  " + str(time.time()-start))

    # full data is the maximum of the data shape and chunk shape dimensions
    start = time.time()
    fulldata = np.full(cshape + data.shape[1:], np.nan, dtype=data.dtype)
    # logging.info("Time to full " + col + "  " + str(time.time()-start))

    start = time.time()
    fulldata[tidxs, bidxs] = data
    # logging.info("Time to reorganize " + col + "  " + str(time.time()-start))

    return fulldata


def create_attribute_metadata(col, tb_tool):
    # Still a lot to do.
    attrs_metadata = {}

    if col == "UVW":
        # Should not be hardcoded
        attrs_metadata["type"] = "uvw"
        attrs_metadata["units"] = "m"
        attrs_metadata["description"] = "uvw coordinates."

    return attrs_metadata


def create_coordinates(
    xds, infile, ddi, utime, interval, baseline_ant1_id, baseline_ant2_id
):
    coords = {
        "time": utime,
        "baseline_antenna1_id": ("baseline_id", baseline_ant1_id),
        "baseline_antenna2_id": ("baseline_id", baseline_ant2_id),
        "uvw_label": ["u", "v", "w"],
        "baseline_id": np.arange(len(baseline_ant1_id)),
    }

    ddi_xds = read_generic_table(infile, "DATA_DESCRIPTION").sel(row=ddi)
    pol_setup_id = ddi_xds.polarization_id.values
    spw_id = ddi_xds.spectral_window_id.values

    spw_xds = read_generic_table(
        infile,
        "SPECTRAL_WINDOW",
        rename_ids=subt_rename_ids["SPECTRAL_WINDOW"],
    ).sel(spectral_window_id=spw_id)
    coords["frequency"] = spw_xds["chan_freq"].data[
        ~(np.isnan(spw_xds["chan_freq"].data))
    ]

    pol_xds = read_generic_table(
        infile,
        "POLARIZATION",
        rename_ids=subt_rename_ids["POLARIZATION"],
    )
    num_corr = int(pol_xds["num_corr"][pol_setup_id].values)
    coords["polarization"] = np.vectorize(stokes_types.get)(
        pol_xds["corr_type"][pol_setup_id, :num_corr].values
    )

    xds = xds.assign_coords(coords)

    # Add metadata to coordinates:
    # measures_freq_ref = spw_xds["meas_freq_ref"].data
    xds.frequency.attrs["type"] = "spectral_coord"
    xds.frequency.attrs["units"] = spw_xds.attrs["other"]["msv2"]["ctds_attrs"][
        "column_descriptions"
    ]["CHAN_FREQ"]["keywords"]["QuantumUnits"][0]
    # xds.frequency.attrs["velocity_frame"] = spw_xds.attrs["other"]["msv2"][
    #     "ctds_attrs"
    # ]["column_descriptions"]["CHAN_FREQ"]["keywords"]["MEASINFO"]["TabRefTypes"][
    #     measures_freq_ref
    # ]
    xds.frequency.attrs["spectral_window_name"] = str(spw_xds.name.values)
    xds.frequency.attrs["effective_channel_width"] = "EFFECTIVE_CHANNEL_WIDTH"
    # Add if doppler table is present
    # xds.frequency.attrs["doppler_velocity"] =
    # xds.frequency.attrs["doppler_type"] =

    unique_chan_width = np.unique(spw_xds.chan_width.data[np.logical_not(np.isnan(spw_xds.chan_width.data))])
    # print('unique_chan_width',unique_chan_width)
    # print('spw_xds.chan_width.data',spw_xds.chan_width.data)
    #assert len(unique_chan_width) == 1, "Channel width varies for spw."
    #xds.frequency.attrs["channel_width"] = spw_xds.chan_width.data[
    #    ~(np.isnan(spw_xds.chan_width.data))
    #]  # unique_chan_width[0]
    xds.frequency.attrs["channel_width"] = {"dims":"", "data":np.abs(unique_chan_width[0]), "attrs":{"type":"quanta","units":"Hz"}} #Should always be increasing (ordering is fixed before saving).

    main_table_attrs = extract_table_attributes(infile)
    xds.time.attrs["type"] = "time"
    xds.time.attrs["units"] = main_table_attrs["column_descriptions"]["TIME"][
        "keywords"
    ]["QuantumUnits"][0]
    xds.time.attrs["time_scale"] = main_table_attrs["column_descriptions"]["TIME"][
        "keywords"
    ]["MEASINFO"]["Ref"]
    xds.time.attrs[
        "format"
    ] = "unix"  # Time gets converted to unix in xradio.vis._vis_utils._ms._tables.read.convert_casacore_time
    xds.time.attrs["integration_time"] = {"dims":"", "data":interval, "attrs":{"type":"quanta","units":"s"}}
    xds.time.attrs["effective_integration_time"] = "EFFECTIVE_INTEGRATION_TIME"

    return xds


def convert_and_write_partition(
    infile: str,
    outfile: str,
    ddi: int = 0,
    field_id: int = None,
    ignore_msv2_cols: Union[list, None] = None,
    chunks_on_disk: Union[Dict, None] = None,
    compressor: numcodecs.abc.Codec = numcodecs.Zstd(level=2),
    storage_backend="zarr",
    overwrite: bool = False,
):
    if ignore_msv2_cols is None:
        ignore_msv2_cols = []

    file_name = (
        outfile
    )
    taql_where = f"where (DATA_DESC_ID = {ddi})"



    if field_id is not None:
        taql_where += f" AND (FIELD_ID = {field_id})"
        file_name = file_name + "_field_id_" + str(field_id)

    ddi_xds = read_generic_table(infile, "DATA_DESCRIPTION").sel(row=ddi)
    spw_id = ddi_xds.spectral_window_id.values

    spw_xds = read_generic_table(
        infile,
        "SPECTRAL_WINDOW",
        rename_ids=subt_rename_ids["SPECTRAL_WINDOW"],
    ).sel(spectral_window_id=spw_id)
    n_chan = len(spw_xds["chan_freq"].data[~(np.isnan(spw_xds["chan_freq"].data))])

    start_with = time.time()
    with open_table_ro(infile) as mtable:
        # one partition, select just the specified ddi (+ scan/subscan)
        taql_main = f"select * from $mtable {taql_where}"
        with open_query(mtable, taql_main) as tb_tool:
            # print(taql_where,file_name,"Flag shape",tb_tool.getcol('FLAG').shape)

            if tb_tool.nrows() == 0:
                tb_tool.close()
                mtable.close()
                return xr.Dataset(), {}, {}

            # logging.info("Setting up table "+ str(time.time()-start_with))

            start = time.time()
            (
                tidxs,
                bidxs,
                didxs,
                baseline_ant1_id,
                baseline_ant2_id,
                utime,
            ) = calc_indx_for_row_split(tb_tool, taql_where)
            time_baseline_shape = (len(utime), len(baseline_ant1_id))
            # logging.info("Calc indx for row split "+ str(time.time()-start))

            start = time.time()
            xds = xr.Dataset()
            col_to_data_variable_names = {
                "FLOAT_DATA" : "SPECTRUM",
                "DATA": "VISIBILITY",
                "CORRECTED_DATA": "VISIBILITY_CORRECTED",
                "WEIGHT_SPECTRUM": "WEIGHT",
                "WEIGHT": "WEIGHT",
                "FLAG": "FLAG",
                "UVW": "UVW",
                "TIME_CENTROID": "TIME_CENTROID",
                "EXPOSURE": "EFFECTIVE_INTEGRATION_TIME",
            }
            col_dims = {
                "DATA": ("time", "baseline_id", "frequency", "polarization"),
                "CORRECTED_DATA": ("time", "baseline_id", "frequency", "polarization"),
                "WEIGHT_SPECTRUM": ("time", "baseline_id", "frequency", "polarization"),
                "WEIGHT": ("time", "baseline_id", "frequency", "polarization"),
                "FLAG": ("time", "baseline_id", "frequency", "polarization"),
                "UVW": ("time", "baseline_id", "uvw_label"),
                "TIME_CENTROID": ("time", "baseline_id"),
                "EXPOSURE": ("time", "baseline_id"),
                "FLOAT_DATA": ("time", "baseline_id", "frequency", "polarization"),
            }
            col_to_coord_names = {
                "TIME": "time",
                "ANTENNA1": "baseline_ant1_id",
                "ANTENNA2": "baseline_ant2_id",
            }
            coords_dim_select = {
                "TIME": np.s_[:, 0:1],
                "ANTENNA1": np.s_[0:1, :],
                "ANTENNA2": np.s_[0:1, :],
            }
            check_variables = {}

            col_names = tb_tool.colnames()

            # Create Data Variables
            # logging.info("Setup xds "+ str(time.time()-start))
            for col in col_names:
                if col in col_to_data_variable_names:
                    if (col == "WEIGHT") and ("WEIGHT_SPECTRUM" in col_names):
                        continue
                    try:
                        start = time.time()
                        if col == "WEIGHT":
                            xds[col_to_data_variable_names[col]] = xr.DataArray(
                                np.tile(
                                    read_col(
                                        tb_tool,
                                        col,
                                        time_baseline_shape,
                                        tidxs,
                                        bidxs,
                                        didxs,
                                    )[:, :, None, :],
                                    (1, 1, n_chan, 1),
                                ),
                                dims=col_dims[col],
                            )

                        else:
                            xds[col_to_data_variable_names[col]] = xr.DataArray(
                                read_col(
                                    tb_tool,
                                    col,
                                    time_baseline_shape,
                                    tidxs,
                                    bidxs,
                                    didxs,
                                ),
                                dims=col_dims[col],
                            )
                            # logging.info("Time to read column " + str(col) + " : " + str(time.time()-start))
                    except:
                        # logging.debug("Could not load column",col)
                        continue

                    xds[col_to_data_variable_names[col]].attrs.update(
                        create_attribute_metadata(col, tb_tool)
                    )

            field_id = _check_single_field(tb_tool)
            interval = _check_interval_consistent(tb_tool)

            start = time.time()

            xds = create_coordinates(
                xds, infile, ddi, utime, interval, baseline_ant1_id, baseline_ant2_id
            )

            field_xds = read_generic_table(
                infile,
                "FIELD",
                rename_ids=subt_rename_ids["FIELD"],
            )
            
            delay_dir = {"dims":"", "data":list(field_xds["delay_dir"].data[field_id, 0, :]), "attrs": {"units": "rad", "type":"sky_coord", "description":"Direction of delay center in right ascension and declination."}}
            phase_dir = {"dims":"", "data":list(field_xds["phase_dir"].data[field_id, 0, :]), "attrs": {"units": "rad", "type":"sky_coord", "description":"Direction of phase center in right ascension and declination."}}
            reference_dir = {"dims":"", "data":list(field_xds["delay_dir"].data[field_id, 0, :]), "attrs": {"units": "rad", "type":"sky_coord", "description":"Direction of reference direction in right ascension and declination. Used in single-dish to record the associated reference direction if position-switching has already been applied. For interferometric data, this is the original correlated field center, and may equal delay_direction or phase_direction."}}

            field_info = {
                "name": field_xds["name"].data[field_id],
                "code": field_xds["code"].data[field_id],
                "delay_direction": delay_dir,
                "phase_direction": phase_dir,
                "reference_direction": reference_dir,
                "field_id": field_id
            }
            xds.attrs["field_info"] = field_info

            xds.attrs["data_groups"] = {
                "base": {
                    "visibility": "VISIBILITY",
                    "flag": "FLAG",
                    "weight": "WEIGHT",
                    "uvw": "UVW",
                }
            }

            if overwrite:
                mode = "w"
            else:
                mode = "w-"

            add_encoding(xds, compressor=compressor, chunks=chunks_on_disk)

            ant_xds = read_generic_table(
                infile,
                "ANTENNA",
                rename_ids=subt_rename_ids["ANTENNA"],
            )
            del ant_xds.attrs["other"]

            xds.attrs["ddi"] = ddi
            
            #Time and frequency should always be increasing
            if xds.frequency[1]-xds.frequency[0] < 0:
                xds = xds.sel(frequency=slice(None, None, -1))
            
            if xds.time[1]-xds.time[0] < 0:
                xds = xds.sel(time=slice(None, None, -1))

            if storage_backend == "zarr":
                xds.to_zarr(store=file_name + "/MAIN", mode=mode)
                ant_xds.to_zarr(store=file_name + "/ANTENNA", mode=mode)
            elif storage_backend == "netcdf":
                # xds.to_netcdf(path=file_name+"/MAIN", mode=mode) #Does not work
                raise

    # logging.info("Saved ms_v4 " + file_name + " in " + str(time.time() - start_with) + "s")


def enumerated_product(*args):
    yield from zip(
        itertools.product(*(range(len(x)) for x in args)), itertools.product(*args)
    )


def convert_msv2_to_processing_set(
    infile: str,
    outfile: str,
    chunks_on_disk: Union[Dict, None] = None,
    compressor: numcodecs.abc.Codec = numcodecs.Zstd(level=2),
    parallel: bool = False,
    storage_backend="zarr",
    overwrite: bool = False,
):
    """ """
    spw_xds = read_generic_table(
        infile,
        "SPECTRAL_WINDOW",
        rename_ids=subt_rename_ids["SPECTRAL_WINDOW"],
    )

    ddi_xds = read_generic_table(infile, "DATA_DESCRIPTION")
    data_desc_ids = np.arange(ddi_xds.dims["row"])

    field_ids = np.arange(read_generic_table(infile, "FIELD").dims["row"])
    # print(state_xds, intents)
    # field_ids = [None]
    field_ids = np.arange(read_generic_table(infile, "FIELD").dims["row"])

    delayed_list = []

    # for ddi, state, field in itertools.product(data_desc_ids, field_ids):
    #    logging.info("DDI " + str(ddi) + ", STATE " + str(state) + ", FIELD " + str(field))

    for idx, pair in enumerated_product(data_desc_ids, field_ids):
        ddi, field_id = pair

        if parallel:
            delayed_list.append(
                dask.delayed(convert_and_write_partition)(
                    infile,
                    outfile,
                    ddi,
                    field_id,
                    ignore_msv2_cols=ignore_msv2_cols,
                    chunks_on_disk=chunks_on_disk,
                    compressor=compressor,
                    overwrite=overwrite,
                )
            )
        else:
            convert_and_write_partition(
                infile,
                outfile,
                ddi,
                field_id,
                ignore_msv2_cols=ignore_msv2_cols,
                chunks_on_disk=chunks_on_disk,
                compressor=compressor,
                storage_backend=storage_backend,
                overwrite=overwrite,
            )

    if parallel:
        dask.compute(delayed_list)
