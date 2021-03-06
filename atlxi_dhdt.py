# -*- coding: utf-8 -*-
# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:hydrogen
#     text_representation:
#       extension: .py
#       format_name: hydrogen
#       format_version: '1.3'
#       jupytext_version: 1.5.1
#   kernelspec:
#     display_name: deepicedrain
#     language: python
#     name: deepicedrain
# ---

# %% [markdown]
# # **ICESat-2 ATL11 Rate of Height change over Time (dhdt)**
#
# This Jupyter notebook will cover the calculation of
# Ice Height Changes (dh) over Time (dt) using Linear Regression.
# Focusing on the Antarctic continent, for the ICESat-2 time period.
# To save on computational resources, we'll run through a few preprocessing steps:
#
# 1. Select essential points
#   - Subset to geographic region of interest (optional)
#   - Drop points without at least 2 valid heights across all ICESat-2 cycles
# 2. Calculate height range (h_range)
#   - Done for points which are valid and in geographic region of interest
#   - Uses the `deepicedrain.nanptp` function
# 3. Calculate rate of height change over time (dhdt)
#   - Done for points with `h_range > 0.25 metres`
#   - Uses the `deepicedrain.nan_linregress` function
#
# Adapted from https://github.com/suzanne64/ATL11/blob/master/plotting_scripts/AA_dhdt_map.ipynb

# %%
import dask
import datashader
import intake
import numpy as np
import pandas as pd
import pygmt
import scipy.stats
import xarray as xr

import deepicedrain

# %%
client = dask.distributed.Client(n_workers=72, threads_per_worker=1)
client

# %% [markdown]
# # Select essential points

# %%
# Xarray open_dataset preprocessor to add fields based on input filename.
add_path_to_ds = lambda ds: ds.assign_coords(
    coords=intake.source.utils.reverse_format(
        format_string="ATL11.001z123/ATL11_{referencegroundtrack:04d}1x_{mincycle:02d}{maxcycle:02d}_{}_{}.zarr",
        resolved_string=ds.encoding["source"],
    )
)

# Load ATL11 data from Zarr
ds: xr.Dataset = xr.open_mfdataset(
    paths="ATL11.001z123/ATL11_*_003_01.zarr",
    chunks={"cycle_number": 7},
    engine="zarr",
    combine="nested",
    concat_dim="ref_pt",
    parallel="True",
    preprocess=add_path_to_ds,
    backend_kwargs={"consolidated": True},
)

# %% [markdown]
# ## Light pre-processing
#
# - Reproject longitude/latitude to EPSG:3031 x/y
# - Mask out low quality height data

# %%
# Calculate the EPSG:3031 x/y projection coordinates
ds["x"], ds["y"] = deepicedrain.lonlat_to_xy(
    longitude=ds.longitude, latitude=ds.latitude
)
# Set x, y, x_atc and y_atc as coords of the xarray.Dataset instead of lon/lat
ds: xr.Dataset = ds.set_coords(names=["x", "y", "x_atc", "y_atc"])
ds: xr.Dataset = ds.reset_coords(names=["longitude", "latitude"])


# %%
# Mask out low quality height data
ds["h_corr"]: xr.DataArray = ds.h_corr.where(cond=ds.fit_quality == 0)

# %% [markdown]
# ## Trim out unnecessary values (optional)
#
# There's ~220 million ATL11 points for the whole of Antarctica,
# and not all of them will be needed depending on what you want to do.
# To cut down on the number of data points the computer needs to work on,
# we can:
#
# - Subset to geographic region of interest
# - Ensure there are at least 2 height values to calculate trend over time

# %%
# Dictionary of Antarctic bounding box locations with EPSG:3031 coordinates
regions: dict = {
    "kamb": deepicedrain.Region(
        name="Kamb Ice Stream",
        xmin=-411054.19240523444,
        xmax=-365489.6822096751,
        ymin=-739741.7702261859,
        ymax=-699564.516934089,
    ),
    "antarctica": deepicedrain.Region(
        "Antarctica", -2700000, 2800000, -2200000, 2300000
    ),
    "siple_coast": deepicedrain.Region(
        "Siple Coast", -1000000, 250000, -1000000, -100000
    ),
    "whillans": deepicedrain.Region(
        "Whillans Ice Stream", -350000, -100000, -700000, -450000
    ),
    "whillans2": deepicedrain.Region(
        "Whillans Ice Stream", -500000, -400000, -600000, -500000
    ),
}

# %%
# Subset dataset to geographic region of interest
placename: str = "antarctica"
region: deepicedrain.Region = regions[placename]
# ds = region.subset(ds=ds)

# %%
# We need at least 2 points to draw a trend line or compute differences
# So let's drop points with less than 2 valid values across all cycles
# Will take maybe 10-15 min to trim down ~220 million points to ~190 million
print(f"Originally {len(ds.ref_pt)} points")
# ds: xr.Dataset = ds.dropna(dim="ref_pt", thresh=2, subset=["h_corr"])
print(f"Trimmed to {len(ds.ref_pt)} points")

# %% [markdown]
# ### Optimize dataset for big calculations later
#
# We'll rechunk the dataset to a reasonable chunk size,
# and persist key dataset variables in memory so that the parallel
# computations will be more efficient in later sections.

# %%
ds["h_corr"] = ds.h_corr.unify_chunks()

# %%
# Persist the height and time data in distributed memory
ds["h_corr"] = ds.h_corr.persist()
ds["delta_time"] = ds.delta_time.persist()

# %% [markdown]
# ### Retrieve some basic information for plots later
#
# Simply getting the number of cycles and date range
# to put into our plots later on

# %%
# Get number of ICESat-2 cycles used
num_cycles: int = len(ds.cycle_number)

# %%
# Get first and last dates to put into our plots
min_date, max_date = ("2018-10-14", "2020-05-13")
if min_date is None:
    min_delta_time = np.nanmin(ds.delta_time.isel(cycle_number=0).data).compute()
    min_utc_time = deepicedrain.deltatime_to_utctime(min_delta_time)
    min_date: str = np.datetime_as_string(arr=min_utc_time, unit="D")
if max_date is None:
    max_delta_time = np.nanmax(ds.delta_time.isel(cycle_number=-1).data).compute()
    max_utc_time = deepicedrain.deltatime_to_utctime(max_delta_time)
    max_date: str = np.datetime_as_string(arr=max_utc_time, unit="D")
print(f"Handling {num_cycles} ICESat-2 cycles from {min_date} to {max_date}")


# %%

# %% [markdown]
# # Calculate height range (h_range)
#
# A simple way of finding active subglacial lakes is to see where
# there has been a noticeably rapid change in elevation over
# a short period of time such as 2-5 metres a year (or ~4x91-day ICESat-2 cycles).
# 'Range of height' is quick way to do this,
# basically just doing maximum height minus minimum height.

# %%
# Calculate height range across cycles, parallelized using dask
ds["h_range"]: xr.DataArray = xr.apply_ufunc(
    deepicedrain.nanptp,  # min point to max point (range) that handles NaN values
    ds.h_corr,
    input_core_dims=[["cycle_number"]],
    dask="allowed",
    output_dtypes=[ds.h_corr.dtype],
    kwargs={"axis": 1},
)

# %%
# %%time
# Compute height range. Also include all height and time info
ds_ht: xr.Dataset = ds[["h_range", "h_corr", "delta_time"]].compute()

# %%
# Non-parallelized
# h_range = deepicedrain.nanptp(a=ds.h_corr[0:1], axis=1)
# Ensure no height range values which are zero (usually due to only 1 data point)
# assert len(dask.array.argwhere(dsh.h_range <= 0.0).compute()) == 0

# %%
# Save or Load height range data
# ds_ht.to_zarr(store=f"ATLXI/ds_hrange_time_{placename}.zarr", mode="w", consolidated=True)
ds_ht: xr.Dataset = xr.open_dataset(
    filename_or_obj=f"ATLXI/ds_hrange_time_{placename}.zarr",
    chunks={"cycle_number": 7},
    engine="zarr",
    backend_kwargs={"consolidated": True},
)
# ds: xr.Dataset = ds_ht  # shortcut for dhdt calculation later

# %%
df_hr: pd.DataFrame = ds_ht.h_range.to_dataframe()

# %%
df_hr.describe()

# %%
# Datashade our height values (vector points) onto a grid (raster image)
agg_grid: xr.DataArray = region.datashade(df=df_hr, z_dim="h_range")
print(agg_grid)

# %%
# Plot our map!
scale: int = region.scale
fig = pygmt.Figure()
pygmt.makecpt(cmap="tokyo", series=[0.5, 5, 0.5], reverse=True)
fig.grdimage(
    grid=agg_grid,
    region=region.bounds(),
    projection=f"x1:{scale}",
    frame=["afg", f'WSne+t"ICESat-2 Ice Surface Height Range over {region.name}"'],
    Q=True,
)
fig.colorbar(
    position="JCR+e",
    frame=["af", f'x+l"height range from {min_date} to {max_date}"', "y+lm"],
)
# for subglacial_lake in subglacial_lakes:
#     fig.plot(data=subglacial_lake, L=True, pen="thin")
fig.coast(
    region=region.bounds(),
    projection=f"s0/-90/-71/1:{scale}",
    area_thresh="+ag",
    resolution="i",
    shorelines="0.5p",
    V="q",
)
fig.savefig(f"figures/plot_atl11_hrange_{placename}_{min_date}_{max_date}.png")
fig.show(width=600)

# %%

# %% [markdown]
# # Calculate rate of height change over time (dhdt)
#
# Performing linear regression in parallel.
# Uses the [`scipy.stats.linregress`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.linregress.html) function,
# parallelized with xarray's [`apply_ufunc`](http://xarray.pydata.org/en/v0.15.1/examples/apply_ufunc_vectorize_1d.html) method
# on a Dask cluster.

# %%
# Take only the points where there is more than 0.25 metres of elevation change
# Trim down ~220 million points to ~36 million
ds = ds.where(cond=ds.h_range > 0.25, drop=True)
print(f"Trimmed to {len(ds.ref_pt)} points")

# %%
# Do linear regression on many datapoints, parallelized using dask
dhdt_params: xr.DataArray = xr.apply_ufunc(
    deepicedrain.nan_linregress,
    ds.delta_time.astype(np.uint64),  # x is time in nanoseconds
    ds.h_corr,  # y is height in metres
    input_core_dims=[["cycle_number"], ["cycle_number"]],
    output_core_dims=[["dhdt_parameters"]],
    # output_core_dims=[["slope_ns"], ["intercept"], ["r_value"], ["p_value"], ["std_err"]],
    dask="parallelized",
    vectorize=True,
    output_dtypes=[np.float32],
    output_sizes={"dhdt_parameters": 5},
    # output_sizes={"slope_ns":1, "intercept":1, "r_value":1, "p_value":1, "std_err":1}
)

# %%
# Construct an xarray.Dataset containing time, height, and dhdt variables
ds_dhdt: xr.Dataset = ds[["delta_time", "h_corr"]]
for var_name, dataarray in zip(
    ["slope", "intercept", "r_value", "p_value", "std_err"], dhdt_params.transpose()
):
    ds_dhdt[f"dhdt_{var_name}"]: xr.DataArray = dataarray

# %%
# Convert dhdt_slope units from metres per nanosecond to metres per year
# 1 year = 365.25 days x 24 hours x 60 min x 60 seconds x 1_000_000_000 nanoseconds
ds_dhdt["dhdt_slope"] = ds_dhdt["dhdt_slope"] * (365.25 * 24 * 60 * 60 * 1_000_000_000)

# %%
# %%time
# Compute rate of height change over time (dhdt). Also include all height and time info
ds_dhdt: xr.Dataset = ds_dhdt.compute()

# %%
# Do linear regression on single datapoint
# slope_ns, intercept, r_value, p_value, std_err = nan_linregress(
#     x=ds.delta_time[:1].data.astype(np.uint64), y=ds.h_corr[:1].data
# )
# print(slope_ns, intercept, r_value, p_value, std_err)

# %%
# Load or Save rate of height change over time (dhdt) data
# ds_dhdt.to_zarr(store=f"ATLXI/ds_dhdt_{placename}.zarr", mode="w", consolidated=True)
ds_dhdt: xr.Dataset = xr.open_dataset(
    filename_or_obj=f"ATLXI/ds_dhdt_{placename}.zarr",
    chunks={"cycle_number": 7},
    engine="zarr",
    backend_kwargs={"consolidated": True},
)

# %%
df_slope: pd.DataFrame = ds_dhdt.dhdt_slope.to_dataframe()

# %%
# Datashade our height values (vector points) onto a grid (raster image)
agg_grid: xr.DataArray = region.datashade(df=df_slope, z_dim="dhdt_slope")
print(agg_grid)

# %%
# Plot our map!
scale: int = region.scale
fig = pygmt.Figure()
pygmt.makecpt(cmap="roma", series=[-5, 5, 0.5])
fig.grdimage(
    grid=agg_grid,
    region=region.bounds(),
    projection=f"x1:{scale}",
    frame=[
        "afg",
        f'WSne+t"ICESat-2 Change in Ice Surface Height over Time at {region.name}"',
    ],
    Q=True,
)
fig.colorbar(
    position="JCR+e",
    frame=["af", f'x+l"dH/dt from {min_date} to {max_date}"', "y+lm/yr"],
)
# for subglacial_lake in subglacial_lakes:
#    fig.plot(data=subglacial_lake, L=True, pen="thinnest")
fig.coast(
    region=region.bounds(),
    projection=f"s0/-90/-71/1:{scale}",
    area_thresh="+ag",
    resolution="i",
    shorelines="0.5p",
    V="q",
)
fig.savefig(f"figures/plot_atl11_dhdt_{placename}_{min_date}_{max_date}.png")
fig.show(width=600)

# %%

# %% [markdown]
# # Along track plots of subglacial lake drainage/filling events
#
# Let's take a closer look at one potential
# subglacial lake filling event at Whillans Ice Stream.
# We'll plot a cross-section view of
# ice surface height changes over time,
# along an ICESat-2 reference ground track.

# %%
import holoviews as hv
import hvplot.pandas
import panel as pn

# %%
# Subset dataset to geographic region of interest
placename: str = "whillans2"
region: deepicedrain.Region = regions[placename]
ds_subset: xr.Dataset = region.subset(ds=ds_dhdt)

# %%
# Quick facet plot of height over different cycles
# See https://xarray.pydata.org/en/stable/plotting.html#datasets
ds_subset.plot.scatter(
    x="x", y="y", hue="h_corr", cmap="gist_earth", col="cycle_number", col_wrap=4
)

# %%
# Find reference ground tracks that have data up to cycle 7 (the most recent cycle)
rgts = np.unique(
    ar=ds_subset.sel(cycle_number=7).dropna(dim="ref_pt").referencegroundtrack
)
rgts

# %%
# Convert xarray.Dataset to pandas.DataFrame for easier analysis
df_many: pd.DataFrame = ds_subset.to_dataframe().dropna()


# %%
def dhdt_plot(
    cycle: int = 7,
    dhdt_variable: str = "dhdt_slope",
    dhdt_range: tuple = (1, 10),
    rasterize: bool = False,
    datashade: bool = False,
) -> hv.element.chart.Scatter:
    """
    ICESat-2 rate of height change over time (dhdt) interactive scatter plot.
    Uses HvPlot, and intended to be used inside a Panel dashboard.
    """
    df_ = df_many.query(
        expr="cycle_number == @cycle & "
        "abs(dhdt_slope) > @dhdt_range[0] & abs(dhdt_slope) < @dhdt_range[1]"
    )
    return df_.hvplot.scatter(
        title=f"ICESat-2 Cycle {cycle} {dhdt_variable}",
        x="x",
        y="y",
        c=dhdt_variable,
        cmap="gist_earth" if dhdt_variable == "h_corr" else "BrBG",
        clim=None,
        # by="cycle_number",
        rasterize=rasterize,
        datashade=datashade,
        dynspread=datashade,
        hover=True,
        hover_cols=["referencegroundtrack", "dhdt_slope", "h_corr"],
        colorbar=True,
    )


# %%
# Interactive holoviews scatter plot to find referencegroundtrack needed
# Tip: Hover over the points, and find those with high 'dhdt_slope' values
layout: pn.layout.Column = pn.interact(
    dhdt_plot,
    cycle=pn.widgets.IntSlider(name="Cycle Number", start=2, end=7, step=1, value=7),
    dhdt_variable=pn.widgets.RadioButtonGroup(
        name="dhdt_variables",
        value="dhdt_slope",
        options=["referencegroundtrack", "dhdt_slope", "h_corr"],
    ),
    dhdt_range=pn.widgets.RangeSlider(
        name="dhdt range ±", start=0, end=20, value=(1, 10), step=0.5
    ),
    rasterize=pn.widgets.Checkbox(name="Rasterize"),
    datashade=pn.widgets.Checkbox(name="Datashade"),
)
dashboard: pn.layout.Column = pn.Column(
    pn.Row(
        pn.Column(layout[0][1], align="center"),
        pn.Column(layout[0][0], layout[0][2], align="center"),
        pn.Column(layout[0][3], layout[0][4], align="center"),
    ),
    layout[1],
)
dashboard

# %%
# Show dashboard in another browser tab
# dashboard.show()

# %%
# Select one Reference Ground track to look at
rgt: int = 135
assert rgt in rgts
df_rgt: pd.DataFrame = df_many.query(expr="referencegroundtrack == @rgt")
print(f"Looking at Reference Ground Track {rgt}")

# %%
# Select one laser pair (out of three) based on y_atc field
# df = df_rgt.query(expr="y_atc < -100")  # left
df = df_rgt.query(expr="abs(y_atc) < 100")  # centre
# df = df_rgt.query(expr="y_atc > 100")  # right

# %%
# Interactive scatter plot of height along one laser pair track, over time
df.hvplot.scatter(
    x="x_atc",
    y="h_corr",
    by="cycle_number",
    hover=True,
    hover_cols=["x", "y", "dhdt_slope"],
)

# %%
# Filter points to those with significant dhdt values > +/- 0.2 m/yr
df = df.query(expr="abs(dhdt_slope) > 0.2")

# %%
# Plot 2D along track view of
# Ice Surface Height Changes over Time
fig = pygmt.Figure()
# Setup map frame, title, axis annotations, etc
fig.basemap(
    projection="X20c/10c",
    region=[df.x_atc.min(), df.x_atc.max(), df.h_corr.min(), df.h_corr.max()],
    frame=[
        rf'WSne+t"ICESat-2 Change in Ice Surface Height over Time at {region.name}"',
        'xaf+l"Along track x (m)"',
        'yaf+l"Height (m)"',
    ],
)
fig.text(
    x=df.x_atc.mean(),
    y=df.h_corr.max(),
    text=f"Reference Ground Track {rgt:04d}",
    justify="TC",
    D="jTC-0c/0.2c",
)

# Colors from https://colorbrewer2.org/#type=qualitative&scheme=Set1&n=7
cycle_colors = {3: "#ff7f00", 4: "#984ea3", 5: "#4daf4a", 6: "#377eb8", 7: "#e41a1c"}
for cycle, color in cycle_colors.items():
    df_ = df.query(expr="cycle_number == @cycle").copy()
    if len(df_) > 0:
        # Get x, y, time
        data = np.column_stack(tup=(df_.x_atc, df_.h_corr))
        time_nsec = deepicedrain.deltatime_to_utctime(dataarray=df_.delta_time.mean())
        time_sec = np.datetime_as_string(arr=time_nsec.to_datetime64(), unit="s")
        label = f'"Cycle {cycle} at {time_sec}"'

        # Plot data points
        fig.plot(data=data, style="c0.05c", color=color, label=label)
        # Plot line connecting points
        # fig.plot(data=data, pen=f"faint,{color},-", label=f'"+g-1l+s0.15c"')

fig.legend(S=3, position="jBR+jBR+o0.2c", box="+gwhite+p1p")
fig.savefig(f"figures/alongtrack_atl11_dh_{placename}_{rgt}.png")
fig.show()

# %%
